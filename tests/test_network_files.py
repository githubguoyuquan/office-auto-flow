import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from pptx_service import validate_selected_paths
import test_update_workbench as fixtures


class NetworkFileTests(unittest.TestCase):
    def test_windows_unc_and_mapped_drive_are_preserved(self):
        paths = [r"\\server\共享\工艺单.pptx", r"Z:\共享\工艺单.pptx"]
        with patch("pptx_service.sys.platform", "win32"), patch("pptx_service.validate_file", side_effect=lambda value, kind: value):
            self.assertEqual(validate_selected_paths([f'"{value}"' for value in paths], "pptx"), paths)
        with patch("pptx_service.sys.platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "Windows 客户端"):
                validate_selected_paths(paths, "pptx")

    def test_direct_paths_validate_count_kind_existence_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "中文 空格.pptx"
            file.write_bytes(b"test")
            self.assertEqual(validate_selected_paths([str(file), str(file)], "pptx"), [str(file.resolve())])
            for paths, kind, options in (([], "pptx", {}), ([str(file)], [], {}),
                                         ([str(file)], "xlsx", {}), (["missing.pptx"], "pptx", {}),
                                         ([str(file), str(file)], "pptx", {"multiple": False})):
                with self.assertRaises(ValueError):
                    validate_selected_paths(paths, kind, **options)

    def test_authenticated_direct_file_routes_and_xlsx_upload_bridge(self):
        fixture = fixtures.WorkbenchUpdateTests()
        fixture.setUp()
        try:
            file = fixture.root / "来源.xlsx"
            book = Workbook();book.active.append(["辅料编号", "供应商"]);book.save(file);book.close()
            body = {"paths": [str(file)], "kind": "xlsx"}
            self.assertEqual(fixture.request("/api/local-xlsx", body, token=False)[0], 403)
            code, result = fixture.request("/api/local-xlsx", body)
            self.assertEqual(code, 200, result)
            self.assertEqual(base64.b64decode(result["content"]), file.read_bytes())
            self.assertEqual(fixture.request("/api/pptx/files", body)[1]["paths"], [str(file.resolve())])
            self.assertEqual(fixture.request("/api/template-builder/source", body)[1]["fields"], ["辅料编号", "供应商"])
        finally:
            fixture.doCleanups()
