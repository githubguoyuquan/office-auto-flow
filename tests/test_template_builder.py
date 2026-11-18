from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook

from template_builder import NS, collect_labels, create_template, validate_fields
import test_update_workbench as fixtures
import oa_login_cli as app


class TemplateBuilderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_xlsx_uses_headers_only_and_deduplicates_labels(self):
        source = self.root / "source.xlsx"
        book = Workbook()
        book.active.append(["款号", "数量", "款号", None])
        book.active.append(["BUSINESS_VALUE", 123, "DO_NOT_EXPORT", "extra"])
        book.create_sheet("第二表").append(["供应商", "数量"])
        book.save(source)
        book.close()
        original = source.read_bytes()
        self.assertEqual(collect_labels(source), ["款号", "数量", "供应商"])
        self.assertEqual(source.read_bytes(), original)

    def test_pptx_reads_headers_vertical_labels_and_text_labels(self):
        def paragraph(value):
            return "<a:p><a:r><a:t>" + escape(value) + "</a:t></a:r></a:p>"

        def table(rows):
            return "<a:tbl>" + "".join("<a:tr>" + "".join(
                "<a:tc><a:txBody>" + paragraph(value) + "</a:txBody></a:tc>" for value in row)
                + "</a:tr>" for row in rows) + "</a:tbl>"

        source = self.root / "source.pptx"
        xml = (f'<p:sld xmlns:p="p" xmlns:a="{NS}">'
               + table([["辅料编号", "数量"], ["TRIM_SECRET", "7"]])
               + table([["设计师", "PERSON_SECRET"], ["季节", "SUMMER_SECRET"]])
               + table([["属性", "值"], ["自定义标签", "VALUE_SECRET"]])
               + paragraph("正式款号：STYLE_SECRET") + paragraph("OTHER_SECRET") + "</p:sld>")
        with ZipFile(source, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", xml)
        self.assertEqual(collect_labels(source), ["辅料编号", "数量", "设计师", "季节", "自定义标签", "正式款号"])

    def test_template_contains_only_edited_headers_and_blank_data_row(self):
        fields = ["辅料编号", "字段｜备用字段", "=literal label"]
        content = create_template(fields)
        book = load_workbook(BytesIO(content))
        try:
            self.assertEqual(list(book.active.values), [tuple(fields), (None, None, None)])
            self.assertEqual(book.active["C1"].data_type, "s")
            self.assertEqual(book.sheetnames, ["提取模板"])
        finally:
            book.close()

    def test_invalid_fields_or_sources_report_errors(self):
        for fields in ([], [""], ["重复", "重复"], ["bad\x01"], [42]):
            with self.assertRaises(ValueError):
                validate_fields(fields)
        path = self.root / "bad.pptx"
        path.write_bytes(b"not a presentation")
        with self.assertRaisesRegex(ValueError, "无法识别"):
            collect_labels(path)


class TemplateBuilderHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkbenchUpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_file_selection_cancellation_error_and_download(self):
        request = self.fixture.request
        self.assertEqual(request("/api/template-builder/source", {"kind": "xlsx"}, token=False)[0], 403)
        self.assertEqual(request("/api/template-builder/download", {"fields": ["数量"]}, token=False)[0], 403)
        with patch.object(app, "choose_files", return_value=[]):
            self.assertEqual(request("/api/template-builder/source", {"kind": "xlsx"})[1], {"cancelled": True})
        with patch.object(app, "choose_files", side_effect=ValueError("请选择有效文件")):
            self.assertEqual(request("/api/template-builder/source", {"kind": "xlsx"})[0], 400)
        self.assertEqual(request("/api/template-builder/download", {"fields": ["重复", "重复"]})[0], 400)
        self.assertEqual(request("/api/template-builder/download", {"fields": ["数量", "面料"]})[0], 200)
