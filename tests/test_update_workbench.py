import base64
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from zipfile import ZipFile, ZIP_DEFLATED

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import oa_login_cli as app
from offline_updates import BASE_API, PRODUCT, WEB_FILES, canonical
from update_manager import UpdateManager
from ui_resources import WEB_ROOT


class WorkbenchUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = Ed25519PrivateKey.generate()
        self.manager = UpdateManager(lambda: self.root, {
            "test": self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)})
        self.controller = app.AutomationController()
        licensing = Mock()
        licensing.status.return_value = {"valid": True, "unrestricted": True, "features": []}
        for name, value in (("UPDATES", self.manager), ("CONTROLLER", self.controller),
                            ("data_dir", lambda: self.root), ("LICENSES", licensing)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.LocalHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = build_opener(ProxyHandler({}))

    def request(self, path, body=None, *, token=True, raw=False):
        headers = {"X-AIOA-Token": app.ACCESS_TOKEN} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/octet-stream" if raw else "application/json"
            body = body if raw else json.dumps(body).encode()
        request = Request(f"http://127.0.0.1:{self.server.server_port}" + path,
                          data=body, headers=headers)
        try:
            response = self.client.open(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            content = response.read().decode()
            return response.status, json.loads(content) if "application/json" in response.headers["Content-Type"] else content

    def package(self, sequence=1, invalid_js=False):
        files = {name: (WEB_ROOT / Path(name).name).read_bytes() for name in WEB_FILES}
        files["web/workbench.js"] += (b"\nfunction {" if invalid_js else
                                       f"\nwindow.__UPDATE_TEST__ = 'release-{sequence}';".encode())
        manifest = {"format": 1, "product": PRODUCT, "base_api": BASE_API,
                    "sequence": sequence, "version": f"release-{sequence}",
                    "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                              for name, data in files.items()}}
        envelope = {"key_id": "test", "manifest": manifest,
                    "signature": base64.b64encode(self.key.sign(canonical(manifest))).decode()}
        path = self.root / f"update-{sequence}.zip"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("update.json", canonical(envelope))
            for name, data in files.items():
                archive.writestr("payload/" + name, data)
        return path.read_bytes()

    def test_http_install_refresh_rollback_and_recovery_preserve_user_data(self):
        settings = self.root / "supplier-names.json"
        settings.write_text("unchanged")
        code, status = self.request("/api/updates/install", self.package(), raw=True)
        self.assertEqual(code, 200, status)
        self.assertEqual(status["version"], "release-1")
        self.assertTrue(status["can_rollback"])
        self.assertIn("window.__UPDATE_TEST__", self.request("/")[1])
        self.assertNotIn("window.__UPDATE_TEST__", self.request("/updates/recovery")[1])
        restarted = UpdateManager(lambda: self.root, self.manager.keys)
        self.assertIn("release-1", restarted.page("current-session"))
        code, status = self.request("/api/updates/rollback", {})
        self.assertEqual(code, 200, status)
        self.assertEqual(status["version"], "基础版本")
        self.assertNotIn("window.__UPDATE_TEST__", self.request("/")[1])
        self.assertEqual(settings.read_text(), "unchanged")
        self.assertEqual(list(self.root.glob(".update-upload-*")), [])

    def test_authentication_and_running_task_reject_upload(self):
        package = self.package()
        self.assertEqual(self.request("/api/updates/install", package, raw=True, token=False)[0], 403)
        self.assertEqual(self.request("/api/updates/rollback", {}, token=False)[0], 403)
        self.assertEqual(self.request("/api/updates/status", token=False)[0], 404)
        self.controller.task_active = True
        self.assertEqual(self.request("/api/updates/install", package, raw=True)[0], 400)
        self.assertEqual(self.request("/api/updates/rollback", {})[0], 400)
        self.assertFalse(self.controller.updating)
        self.assertIsNone(self.manager.store().state()["active"])

    def test_update_reservation_blocks_task_start_and_releases_on_error(self):
        with self.controller.maintenance():
            self.assertTrue(self.controller.snapshot()["maintenance"])
            with self.assertRaises(ValueError):
                self.controller.start({"name": "test", "module_id": "test"})
            with patch.object(app, "prepare_pptx_request", return_value={}):
                with self.assertRaises(ValueError):
                    self.controller.start({"name": "test", "module_id": "test", "kind": "pptx_extract", "request": {}})
            self.assertEqual(self.request("/api/updates/install", self.package(), raw=True)[0], 400)
        self.assertFalse(self.controller.updating)
        self.assertFalse(self.controller.task_active)
        code, result = self.request("/api/updates/install", self.package(invalid_js=True), raw=True)
        self.assertEqual(code, 400, result)
        self.assertIn("语法错误", result["error"])
        self.assertFalse(self.controller.updating)
        self.assertIsNone(self.manager.store().state()["active"])
        self.assertEqual(list(self.root.glob(".update-upload-*")), [])

    def test_modified_installed_resource_falls_back_and_reports_error(self):
        self.assertEqual(self.request("/api/updates/install", self.package(), raw=True)[0], 200)
        selected, _ = self.manager.selected()
        (selected / "web/pptx.js").write_text("modified")
        self.assertNotIn("window.__UPDATE_TEST__", self.request("/")[1])
        code, status = self.request("/api/updates/status")
        self.assertEqual(code, 200)
        self.assertIn("基础版本", status["error"])
        self.assertEqual(status["version"], "基础版本")
        self.assertEqual(self.request("/api/updates/rollback", {})[0], 200)

    def test_development_build_without_release_key_cannot_install(self):
        self.manager.keys = {}
        code, result = self.request("/api/updates/install", self.package(), raw=True)
        self.assertEqual(code, 400, result)
        self.assertIn("公钥", result["error"])
        self.assertFalse(self.request("/api/updates/status")[1]["enabled"])
