from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from openpyxl.worksheet.print_settings import PrintArea

from trim_template import export_trim_xlsx


class SupplierExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.template = Path(self.temporary.name) / "template.xlsx"
        self.destination = Path(self.temporary.name) / "output.xlsx"

    def make_template(self, headers=("辅料编号", "名称")):
        book = Workbook()
        sheet = book.active
        for column, title in enumerate(headers, 1):
            sheet.cell(2, column, title).fill = PatternFill("solid", fgColor="123456")
            sheet.cell(3, column).number_format = "@"
        sheet.print_area = f"A1:{sheet.cell(4, len(headers)).coordinate}"
        book.save(self.template)
        book.close()

    def test_suppliers_follow_the_same_row_and_duplicate_codes_are_cached(self):
        self.make_template()
        lookup = Mock(side_effect=lambda code: {
            "0001": ("供应商甲", ""), "0002": ("=供应商乙", "供应商丙"),
        }[code])
        tables = [[['辅料编号', '0001', '0002', '0001'], ['名称', '纽扣', '拉链', '备用纽扣']]]
        result = export_trim_xlsx(self.template, tables, self.destination, supplier_lookup=lookup)
        self.assertEqual([call.args[0] for call in lookup.call_args_list], ["0001", "0002"])
        book = load_workbook(self.destination)
        try:
            sheet = book.active
            self.assertEqual([sheet.cell(2, col).value for col in range(1, 5)],
                             ["辅料编号", "名称", "供应商", "供应商2"])
            self.assertEqual([[sheet.cell(row, col).value for col in range(1, 5)] for row in range(3, 6)],
                             [["0001", "纽扣", "供应商甲", None],
                              ["0002", "拉链", "=供应商乙", "供应商丙"],
                              ["0001", "备用纽扣", "供应商甲", None]])
            self.assertEqual(sheet["C4"].data_type, "s")
            self.assertEqual(sheet["C2"]._style, sheet["B2"]._style)
            area = next(iter(PrintArea.from_string(sheet.print_area).ranges))
            self.assertEqual((area.max_col, area.max_row), (4, 5))
        finally:
            book.close()
        self.assertEqual(result["supplier_missing"], 0)

    def test_missing_and_blank_codes_stay_in_their_rows(self):
        self.make_template()
        lookup = Mock(return_value=("", ""))
        result = export_trim_xlsx(self.template,
            [[['辅料编号', '', '0003'], ['名称', '无编号辅料', '待维护辅料']]],
            self.destination, supplier_lookup=lookup)
        lookup.assert_called_once_with("0003")
        self.assertEqual(result["supplier_missing"], 1)
        book = load_workbook(self.destination)
        try:
            self.assertEqual(book.active.max_column, 3)
            self.assertEqual(book.active.cell(2, 3).value, "供应商")
        finally:
            book.close()

    def test_existing_supplier_columns_are_reused(self):
        self.make_template(("辅料编号", "名称", "供应商1", "供应商2"))
        export_trim_xlsx(self.template, [[['辅料编号', '001'], ['名称', '纽扣'], ['颜色', '白']]],
                         self.destination, supplier_lookup=lambda _: ("供应商甲", ""))
        book = load_workbook(self.destination)
        try:
            self.assertEqual(book.active.max_column, 4)
            self.assertEqual(book.active.cell(2, 3).value, "供应商")
            self.assertEqual(book.active.cell(3, 3).value, "供应商甲")
        finally:
            book.close()

    def test_lookup_failure_does_not_replace_an_existing_output(self):
        self.make_template()
        self.destination.write_bytes(b"existing output")
        with self.assertRaisesRegex(RuntimeError, "查询失败"):
            export_trim_xlsx(self.template, [[['辅料编号', '001'], ['名称', '纽扣']]],
                self.destination, supplier_lookup=Mock(side_effect=RuntimeError("查询失败")))
        self.assertEqual(self.destination.read_bytes(), b"existing output")

    def test_lookup_uses_source_code_not_output_template(self):
        self.make_template(headers=("名称", "颜色"))
        lookup = Mock(return_value=("供应商甲", ""))
        export_trim_xlsx(self.template, [[['辅料编号', '001'], ['名称', '纽扣'], ['颜色', '白']]],
                         self.destination, supplier_lookup=lookup)
        lookup.assert_called_once_with('001')
        book = load_workbook(self.destination)
        try:
            self.assertEqual(book.active.cell(3, 1).value, '纽扣')
            self.assertEqual(book.active.cell(3, 3).value, '供应商甲')
            self.assertNotIn('辅料编号', [cell.value for cell in book.active[2]])
        finally:
            book.close()

    def test_real_oa_code_header_ignores_old_material_code(self):
        self.make_template(headers=("Description(描述)", "Color"))
        lookup = Mock(return_value=("供应商甲", ""))
        export_trim_xlsx(self.template, [[
            ['Code(编号)', '2LBWLPE0191', 'TTA1766'],
            ['Description(描述)', '唛头', '蕾丝'], ['Color', 'WHITE', 'A'],
            ['oldMaterialCode', 'TTL1696', 'OLD'], ['Supplier1', '160027', ''],
        ]], self.destination, supplier_lookup=lookup)
        self.assertEqual([call.args[0] for call in lookup.call_args_list], ['2LBWLPE0191', 'TTA1766'])


if __name__ == "__main__":
    unittest.main()
