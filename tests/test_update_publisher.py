import base64
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from zipfile import ZipFile

from offline_updates import UpdateError, verify_manifest
from update_manager import UpdateManager
from update_publisher import Publisher, create_keys, load_key, serve
from ui_resources import WEB_ROOT


class UpdatePublisherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.password = "test-update-password-only"
        self.private, public = create_keys(self.password)
        self.public = {name: base64.b64decode(value) for name, value in public.items()}
        self.publisher = Publisher(self.root / "publisher")

    def test_encrypted_key_and_package_install_roundtrip(self):
        self.assertIn(b"ENCRYPTED PRIVATE KEY", self.private)
        package = self.publisher.issue(WEB_ROOT, "0.3.1", self.private, self.password, 1)
        self.assertLess(len(package), 100_000)
        with ZipFile(io.BytesIO(package)) as archive:
            manifest = verify_manifest(archive.read("update.json"), self.public)
            self.assertEqual(manifest["sequence"], 1)
            self.assertFalse(any("private" in name or "publisher" in name for name in archive.namelist()))
        path = self.root / "signed.oaf-update"
        path.write_bytes(package)
        manager = UpdateManager(lambda: self.root / "client", self.public)
        self.assertEqual(manager.install(path)["version"], "0.3.1")
        self.assertIn("const TOKEN = 'current-token'", manager.page("current-token"))
        manager.rollback()
        self.assertEqual(manager.status()["version"], "基础版本")

    def test_wrong_password_and_unencrypted_key_are_rejected(self):
        with self.assertRaisesRegex(UpdateError, "密码"):
            load_key(self.private, "wrong-password")
        from cryptography.hazmat.primitives import serialization
        key = load_key(self.private, self.password)
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        with self.assertRaises(UpdateError):
            load_key(raw, self.password)
        with self.assertRaises(UpdateError):
            create_keys("short")

    def test_new_full_install_takes_precedence_over_older_resource_updates(self):
        package = self.publisher.issue(WEB_ROOT, "old-overlay", self.private, self.password, 10)
        path = self.root / "update.oaf-update"
        path.write_bytes(package)
        manager = UpdateManager(lambda: self.root / "client", self.public)
        manager.install(path)
        newer_base = UpdateManager(lambda: self.root / "client", self.public, base_sequence=20)
        self.assertEqual(newer_base.status()["version"], "基础版本")
        self.assertIsNone(newer_base.selected()[0])
        with self.assertRaisesRegex(UpdateError, "最低序号：20"):
            newer_base.install(path)
        path.write_bytes(self.publisher.issue(WEB_ROOT, "new-overlay", self.private, self.password, 21))
        self.assertEqual(newer_base.install(path)["version"], "new-overlay")
        newer_base.rollback()
        self.assertEqual(newer_base.status()["version"], "基础版本")

    def test_sequence_is_persistent_and_failed_build_does_not_consume_it(self):
        self.publisher.issue(WEB_ROOT, "first", self.private, self.password, 1)
        restarted = Publisher(self.publisher.directory)
        with self.assertRaisesRegex(UpdateError, "发布序号"):
            restarted.issue(WEB_ROOT, "duplicate", self.private, self.password, 1)
        with self.assertRaises(UpdateError):
            restarted.issue(WEB_ROOT, "bad version", self.private, self.password, 2)
        with patch("update_publisher.make_package", side_effect=UpdateError("test build failure")):
            with self.assertRaises(UpdateError):
                restarted.issue(WEB_ROOT, "failed", self.private, self.password, 2)
        package = restarted.issue(WEB_ROOT, "second", self.private, self.password, 2)
        with ZipFile(io.BytesIO(package)) as archive:
            self.assertEqual(verify_manifest(archive.read("update.json"), self.public)["sequence"], 2)
        package = restarted.issue(WEB_ROOT, "auto", self.private, self.password)
        with ZipFile(io.BytesIO(package)) as archive:
            self.assertGreater(verify_manifest(archive.read("update.json"), self.public)["sequence"], 2)

    def test_source_syntax_and_component_architecture_are_checked(self):
        web = self.root / "web"
        shutil.copytree(WEB_ROOT, web)
        (web / "workbench.js").write_text("function {", encoding="utf-8")
        with self.assertRaisesRegex(UpdateError, "页面不完整|语法错误"):
            self.publisher.issue(web, "bad-js", self.private, self.password, 1)
        component = self.root / "wrong.exe"
        component.write_bytes(b"MZinvalid")
        with self.assertRaisesRegex(UpdateError, "Windows x64"):
            self.publisher.issue(WEB_ROOT, "bad-component", self.private, self.password, 1, component)

    def test_corrupt_ledger_is_not_silently_reset(self):
        self.publisher.directory.mkdir()
        (self.publisher.directory / "release-ledger.json").write_text('{"bad": true}')
        with self.assertRaisesRegex(UpdateError, "发布记录损坏"):
            self.publisher.issue(WEB_ROOT, "test", self.private, self.password)

    def test_http_tool_requires_token_and_does_not_persist_private_keys(self):
        with patch("builtins.print"):
            server = serve(self.publisher, 0, open_browser=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = build_opener(ProxyHandler({}))
        address = f"http://127.0.0.1:{server.server_port}"
        with client.open(address) as response:
            page = response.read().decode()
        token = re.search(r"const TOKEN='([^']+)'", page).group(1)

        def post(path, body, authorized=True):
            headers = {"Content-Type": "application/json", "Origin": address}
            if authorized:
                headers["X-Update-Token"] = token
            try:
                response = client.open(Request(address + path, data=json.dumps(body).encode(), headers=headers))
            except HTTPError as error:
                response = error
            with response:
                return response.status, response.read()

        self.assertEqual(post("/api/keygen", {"password": self.password}, False)[0], 403)
        code, backup = post("/api/keygen", {"password": self.password})
        self.assertEqual(code, 200)
        with ZipFile(io.BytesIO(backup)) as archive:
            self.assertEqual(set(archive.namelist()), {"update-private-key.pem", "update-public-keys.json"})
        body = {"web_root": str(WEB_ROOT), "version": "http-test", "sequence": None,
                "private_key": self.private.decode(), "password": self.password, "component": ""}
        code, package = post("/api/build", body)
        self.assertEqual(code, 200, package[:100])
        with ZipFile(io.BytesIO(package)) as archive:
            self.assertEqual(verify_manifest(archive.read("update.json"), self.public)["version"], "http-test")
        self.assertEqual(list(self.publisher.directory.rglob("*.pem")), [])
        self.assertNotIn(self.password.encode(), (self.publisher.directory / "release-ledger.json").read_bytes())
