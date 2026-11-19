from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import Workbook
from table_sources import preview_tables
import test_update_workbench as workbench_tests
import oa_login_cli as app


class TableSourceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)

    def xlsx(self):
        path=self.root/'测试.xlsx';book=Workbook();sheet=book.active
        sheet.title='合并表头';sheet.append(['基础信息']);sheet.merge_cells('A1:B1')
        sheet.append(['编号','名称']);sheet.append(['001','纽扣']);sheet.append(['002','=1+1'])
        book.save(path);book.close();return path

    def test_xlsx_merge_formula_and_provenance(self):
        path=self.xlsx();before=path.read_bytes();report=preview_tables(path)
        self.assertEqual(path.read_bytes(),before)
        block=report['tables'][0]
        self.assertTrue(any('公式' in warning for warning in block['warnings']))
        item=next(c for c in block['candidates'] if c['orientation']=='rows' and c['header_end']==1)
        self.assertEqual(item['records'][0],('001','纽扣'))
        self.assertEqual(item['sources'][0],((2,0),(2,1)))

    def test_pptx_vertical_table_in_group(self):
        path=self.root/'测试.pptx'
        def row(values):return '<a:tr>'+''.join('<a:tc><a:txBody><a:p><a:r><a:t>'+value+'</a:t></a:r></a:p></a:txBody></a:tc>' for value in values)+'</a:tr>'
        xml='<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:grpSp><a:tbl>'+row(['Code(编号)','001','002'])+row(['Description(描述)','纽扣','蕾丝'])+'</a:tbl></p:grpSp></p:spTree></p:cSld></p:sld>'
        with ZipFile(path,'w') as archive:archive.writestr('ppt/slides/slide2.xml',xml)
        report=preview_tables(path);self.assertEqual(report['tables'][0]['location'],'slide2 · 表 1')
        self.assertTrue(any(c['orientation']=='columns' and c['records'][0]==('001','纽扣') for c in report['tables'][0]['candidates']))

    def test_oversize_sample_reports_truncation(self):
        path=self.root/'多行.xlsx';book=Workbook();sheet=book.active;sheet.append(['编号','名称'])
        for i in range(220):sheet.append([str(i),'名称'+str(i)])
        book.save(path);book.close()
        block=preview_tables(path)['tables'][0]
        self.assertEqual(block['sample_rows'],200);self.assertTrue(block['warnings'])
        self.assertTrue(all(len(c['records'])<=5 for c in block['candidates']))

    def test_empty_pptx_has_explicit_warning(self):
        path=self.root/'空.pptx'
        with ZipFile(path,'w') as archive:archive.writestr('ppt/slides/slide1.xml','<root/>')
        result=preview_tables(path);self.assertFalse(result['tables']);self.assertTrue(result['warnings'])


class TablePreviewHttpTests(unittest.TestCase):
    setUp = workbench_tests.WorkbenchUpdateTests.setUp
    request = workbench_tests.WorkbenchUpdateTests.request
    def test_preview_and_cancel_preserve_source(self):
        path=self.root/'表.xlsx';book=Workbook();book.active.append(['编号','名称']);book.active.append(['001','纽扣']);book.save(path);book.close()
        with patch.object(app,'choose_files',return_value=[str(path)]):
            status,result=self.request('/api/table-recognition/preview',{'kind':'xlsx'})
        self.assertEqual(status,200);self.assertTrue(result['tables'])
        with patch.object(app,'choose_files',return_value=[]):
            self.assertEqual(self.request('/api/table-recognition/preview',{'kind':'xlsx'})[1],{'cancelled':True})
        self.assertEqual(self.request('/api/table-recognition/preview',{'kind':'xlsx'},token=False)[0],403)
