import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from license_issuer import create_keys

ROOT = Path(__file__).resolve().parent.parent


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagingTests(unittest.TestCase):
    def test_update_keys_are_independent_and_embedded_in_generated_module(self):
        import ast
        compiler = load_module("build_compiled")
        _, licenses = create_keys("test-license-password")
        _, updates = create_keys("test-update-password")
        license_keys = {name: base64.b64decode(value) for name, value in licenses.items()}
        update_keys = {name: base64.b64decode(value) for name, value in updates.items()}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            compiler.write_verification_keys(source, license_keys, update_keys, 42)
            tree = ast.parse((source / "update_keys.py").read_text())
            self.assertEqual(ast.literal_eval(tree.body[0].value), update_keys)
            self.assertEqual(ast.literal_eval(tree.body[1].value), 42)
            for invalid in ({}, license_keys):
                with self.assertRaises(ValueError):
                    compiler.write_verification_keys(source, license_keys, invalid)
        self.assertNotIn("update_publisher.py", compiler.APP_MODULES)

    def test_installer_rejects_missing_shared_or_incompatible_update_keys(self):
        from offline_updates import BASE_API
        installer = load_module("build_windows_installer")
        valid = {"public_key_ids": ["a" * 16], "update_public_key_ids": ["b" * 16],
                 "update_base_api": BASE_API, "update_base_sequence": 42}
        installer.validate_update_metadata(valid)
        for change in ({"update_public_key_ids": []}, {"update_public_key_ids": ["a" * 16]},
                       {"update_base_api": BASE_API + 1}, {"update_public_key_ids": ["invalid"]},
                       {"update_base_sequence": 0}):
            with self.assertRaises(ValueError):
                installer.validate_update_metadata(dict(valid, **change))

    def test_only_valid_public_export_is_accepted(self):
        compiler = load_module("build_compiled")
        _, public = create_keys("test-password-only")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "public-keys.json"
            path.write_text(json.dumps(public))
            self.assertEqual(set(compiler.load_public_keys(path)), set(public))
            for value in ({}, {"wrong-id": next(iter(public.values()))}, {"bad": "not base64"}):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    compiler.load_public_keys(path)

    def test_compiled_installer_uses_binary_and_cleans_legacy_entry(self):
        installer = load_module("build_windows_installer")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AIOA.exe").write_bytes(b"test executable placeholder")
            script = installer.build_script(root, root / "setup.exe", "test")
            self.assertIn('MUI_FINISHPAGE_RUN "$INSTDIR\\AIOA.exe"', script)
            self.assertIn('AIOA API 工作台.lnk" "$INSTDIR\\AIOA.exe"', script)
            self.assertIn('Delete "$INSTDIR\\oa_login_cli.py"', script)
            self.assertIn('StrCpy $R1 "$INSTDIR\\AIOA.exe"', script)


if __name__ == "__main__":
    unittest.main()
