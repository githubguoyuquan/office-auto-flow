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
