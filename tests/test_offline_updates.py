import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from offline_updates import BASE_API, PRODUCT, WEB_FILES, UpdateError, UpdateStore, canonical
from ui_resources import WEB_ROOT, render_page


class OfflineUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.key = Ed25519PrivateKey.generate()
        self.store = UpdateStore(self.root / "updates", {
            "test": self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)})

    def package(self, sequence=1, *, change=None, corrupt=False, extra=None):
        files = {name: (WEB_ROOT / Path(name).name).read_bytes() for name in WEB_FILES}
        files["web/workbench.css"] += f"\n/* release {sequence} */".encode()
        manifest = {"format": 1, "product": PRODUCT, "base_api": BASE_API,
                    "version": f"test.{sequence}", "sequence": sequence,
                    "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                              for name, data in files.items()}}
        if change:
            change(manifest)
        envelope = {"manifest": manifest, "key_id": "test",
                    "signature": base64.b64encode(self.key.sign(canonical(manifest))).decode()}
        if corrupt:
            files["web/workbench.css"] = b"x" * len(files["web/workbench.css"])
        path = self.root / f"update-{sequence}.zip"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("update.json", canonical(envelope))
            for name, data in files.items():
                archive.writestr("payload/" + name, data)
            if extra:
                archive.writestr(extra, b"unexpected")
        return path

    @staticmethod
    def health(root):
        page = render_page("test-token", root / "web")
        if "const TOKEN = 'test-token'" not in page:
            raise UpdateError("页面自检失败")

    def test_install_and_rollback_preserve_highest_sequence(self):
        self.store.install(self.package(), self.health)
        first = self.store.state()["active"]
        self.store.install(self.package(2), self.health)
        self.assertEqual(self.store.state()["previous"], first)
        self.store.rollback(self.health)
        self.assertEqual(self.store.state()["active"], first)
        self.assertEqual(self.store.state()["highest_sequence"], 2)
        with self.assertRaisesRegex(UpdateError, "不高于"):
            self.store.install(self.package(2), self.health)
        self.store.rollback(self.health)
        self.assertIsNone(self.store.state()["active"])
        self.assertEqual(self.store.state()["highest_sequence"], 2)

    def test_rejects_tampering_unknown_signer_and_incompatible_base(self):
        for options in ({"corrupt": True}, {"change": lambda m: m.update(base_api=99)},
                        {"change": lambda m: m.update(product="another-app")},
                        {"change": lambda m: m["files"].update({"../outside": {"size": 1, "sha256": "0" * 64}})},
                        {"change": lambda m: m.update(sequence=True)},
                        {"extra": "payload/../../outside"}):
            with self.subTest(options=options), self.assertRaises(UpdateError):
                self.store.install(self.package(**options), self.health)
            self.assertIsNone(self.store.state()["active"])
        self.store.keys = {}
        with self.assertRaisesRegex(UpdateError, "签名"):
            self.store.install(self.package(), self.health)

    def test_bad_signature_and_duplicate_zip_entries(self):
        package = self.package()
        import json
        with ZipFile(package) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        envelope = json.loads(contents["update.json"])
        envelope["manifest"]["version"] = "changed-after-signing"
        contents["update.json"] = canonical(envelope)
        with ZipFile(package, "w") as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(UpdateError, "签名"):
            self.store.install(package, self.health)
        package = self.package()
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(package, "a") as archive:
                archive.writestr("update.json", b"{}")
        with self.assertRaisesRegex(UpdateError, "重复|过多"):
            self.store.install(package, self.health)

    def test_healthcheck_or_disk_failure_keeps_previous_release(self):
        self.store.install(self.package(), self.health)
        state = self.store.state()
        package = self.package(2)
        with self.assertRaisesRegex(RuntimeError, "health failed"):
            self.store.install(package, lambda root: (_ for _ in ()).throw(RuntimeError("health failed")))
        self.assertEqual(self.store.state(), state)
        with self.assertRaisesRegex(UpdateError, "自检失败"):
            self.store.install(package, lambda root: False)
        self.assertEqual(self.store.state(), state)
        with patch("offline_updates.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.store.install(package, self.health)
        self.assertEqual(self.store.state(), state)
        self.assertEqual(len(list((self.store.root / "releases").iterdir())), 1)

    def test_rejects_corrupted_rollback_target(self):
        self.store.install(self.package(), self.health)
        first = self.store.state()["active"]
        self.store.install(self.package(2), self.health)
        state = self.store.state()
        (self.store.root / "releases" / first / "web/pptx.js").write_text("tampered")
        with self.assertRaisesRegex(UpdateError, "校验失败"):
            self.store.rollback(self.health)
        self.assertEqual(self.store.state(), state)

    def test_parallel_operations_are_rejected_and_lock_is_released(self):
        with self.store._lock():
            with self.assertRaisesRegex(UpdateError, "另一个更新"):
                self.store.install(self.package(), self.health)
        self.store.install(self.package(), self.health)

    def test_bad_archive_does_not_modify_state(self):
        package = self.root / "bad.zip"
        package.write_bytes(b"not a zip file")
        with self.assertRaisesRegex(UpdateError, "损坏"):
            self.store.install(package, self.health)
        self.assertIsNone(self.store.state()["active"])
