import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook

import oa_login_cli as app
from material_suppliers import (load_supplier_mapping, read_supplier_mapping,
    save_supplier_mapping, supplier_mapping_status, supplier_names)


def encoded_book(rows, zero_format=None):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    if zero_format:
        book.active["A2"].number_format = zero_format
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return base64.b64encode(stream.getvalue()).decode()


class SupplierMappingTests(unittest.TestCase):
    def test_template_numeric_codes_and_leading_zeros(self):
        content = encoded_book([["供应商编号", "供应商名称"], [123, "供应商甲"], ["160002", "供应商乙"]], "000000")
        mapping = read_supplier_mapping("供应商信息表.xlsx", content)
        self.assertEqual(mapping, {"000123": "供应商甲", "160002": "供应商乙"})
        self.assertEqual(supplier_names(("160002", "000123"), mapping), ("供应商乙", "供应商甲"))

    def test_optional_map_and_missing_entries_fall_back_to_codes(self):
        self.assertEqual(supplier_names(("160002", ""), {}), ("160002", ""))
        self.assertEqual(supplier_names(("160002", "000123"), {"160002": "供应商乙"}),
                         ("供应商乙", "000123"))

    def test_conflicts_formulas_and_incomplete_rows_are_rejected(self):
        for rows in ([['1', '甲'], ['1', '乙']], [['1', '=A1']], [['1', None]], [['供应商编号', '供应商名称']]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                read_supplier_mapping("mapping.xlsx", encoded_book(rows))

    def test_rows_without_supplier_code_reject_the_whole_upload(self):
        with self.assertRaisesRegex(ValueError, "第 2 行缺少供应商编号.*重新上传"):
            read_supplier_mapping("mapping.xlsx",
                encoded_book([["供应商编号", "供应商名称"], [None, "无法匹配的名称"], [160002, "供应商甲"]]))

    def test_atomic_update_preserves_previous_mapping_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            status = save_supplier_mapping(directory, "mapping.xlsx", encoded_book([['160002', '甲']]))
            self.assertEqual(status['count'], 1)
            save_supplier_mapping(directory, "new.xlsx", encoded_book([['160002', '乙']]))
            self.assertEqual(load_supplier_mapping(directory)['entries'], {'160002': '乙'})
            with self.assertRaises(ValueError):
                save_supplier_mapping(directory, "bad.xlsx", encoded_book([['160002', '丙'], ['160002', '丁']]))
            with self.assertRaisesRegex(ValueError, "缺少供应商编号"):
                save_supplier_mapping(directory, "missing.xlsx", encoded_book([[None, '缺少编号'], ['160002', '丙']]))
            self.assertEqual(load_supplier_mapping(directory)['entries'], {'160002': '乙'})
            self.assertEqual(supplier_mapping_status(directory)['filename'], 'new.xlsx')

    def test_missing_or_corrupt_saved_file_can_be_reported_and_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(supplier_mapping_status(directory)['configured'])
            path = Path(directory) / 'supplier-names.json'
            path.write_text('broken')
            self.assertIn('error', supplier_mapping_status(directory))
            with self.assertRaises(ValueError):
                load_supplier_mapping(directory)
            path.unlink()
            self.assertEqual(load_supplier_mapping(directory)['entries'], {})

    def test_process_sheet_print_does_not_capture_supplier_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            save_supplier_mapping(directory, 'mapping.xlsx', encoded_book([['160002', '供应商甲']]))
            controller = app.AutomationController()
            controller.worker = Mock()
            controller.worker.is_alive.return_value = True
            with patch.object(app, 'data_dir', return_value=Path(directory)), patch.object(app.LICENSES, 'require_valid'):
                controller.start({'name': '工艺单打印', 'module_id': 'process_sheet', 'print_settings': {'template_id': 'T'}})
                job = controller.jobs.get_nowait()
                save_supplier_mapping(directory, 'mapping.xlsx', encoded_book([['160002', '供应商乙']]))
                self.assertNotIn('supplier_name_mapping', job)
                self.assertFalse(any('supplier' in key.lower() for key in job))


if __name__ == '__main__':
    unittest.main()
