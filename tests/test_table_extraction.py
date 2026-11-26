import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from openpyxl import Workbook,load_workbook
from table_extraction import normalize_pptx
from table_sources import NS
from pptx_service import invoke, executable, ExtractionCancelled


class FormalTableTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)

    def fixture(self,rows):
        path=self.root/'原始工艺单.pptx'
        table=ET.Element(NS+'tbl')
        for values in rows:
            row=ET.SubElement(table,NS+'tr')
            for value in values:
                cell=ET.SubElement(row,NS+'tc');body=ET.SubElement(cell,NS+'txBody');p=ET.SubElement(body,NS+'p');r=ET.SubElement(p,NS+'r');ET.SubElement(r,NS+'t').text=value
        slide='<p:sld xmlns:p="p" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>TSOW270702</a:t></a:r></a:p>'+ET.tostring(table,encoding='unicode')+'</p:sld>'
        with ZipFile(path,'w') as archive:
            archive.writestr('ppt/presentation.xml','<p:presentation xmlns:p="p" xmlns:r="r"><p:sldIdLst><p:sldId r:id="one"/></p:sldIdLst></p:presentation>')
            archive.writestr('ppt/_rels/presentation.xml.rels','<Relationships><Relationship Id="one" Target="slides/slide1.xml"/></Relationships>')
            archive.writestr('ppt/slides/slide1.xml',slide)
        return path

    def test_formal_extraction_transposes_and_preserves_source(self):
        source=self.fixture([['辅料编号','TRIM001','TRIM002'],['颜色','黑','白'],['数量','2','3']])
        before=hashlib.sha256(source.read_bytes()).digest()
        template=self.root/'模板.xlsx';book=Workbook();book.active.append(['款号','辅料编号','数量']);book.save(template);book.close()
        events=[]
        result=invoke({'action':'extract','inputs':[str(source)],'template':str(template),'output_dir':str(self.root)},threading.Event(),events.append,binary=executable())
        book=load_workbook(result['output']);rows=list(book.active.values);book.close()
        self.assertEqual(rows[0],('款号','辅料编号','数量'))
        self.assertEqual([row[1] for row in rows[1:]],['TRIM001','TRIM002'])
        self.assertEqual([str(row[2]) for row in rows[1:]],['2','3'])
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),before)
        self.assertTrue(any(event.get('input')==str(source) for event in events))
        self.assertFalse(list(self.root.glob('.pptx-*')))

    def test_repeated_sections_merge_without_losing_distinct_rows(self):
        header = ['辅料编号', '颜色', '数量']
        rows = [header, ['001', '黑', '2'], header, ['001', '黑', '2'], ['001', '黑', '3'], ['002', '白', '1']]
        expected = [rows[index] for index in (1, 4, 5)]
        for layout in (rows, list(map(list, zip(*rows)))):
            with self.subTest(transposed=layout is not rows):
                source = self.fixture(layout)
                output = self.root / '副本.pptx'
                self.assertTrue(normalize_pptx(source, output, threading.Event()))
                with ZipFile(output) as archive:
                    slide = ET.fromstring(archive.read('ppt/slides/slide1.xml'))
                values = [[cell.text for cell in row.iter(NS + 't')] for row in slide.iter(NS + 'tr')]
                self.assertEqual(values[1:], expected)

    def test_cancelled_normalization_does_not_write(self):
        source=self.fixture([['辅料编号','001'],['颜色','黑']]);cancel=threading.Event();cancel.set()
        with self.assertRaises(ExtractionCancelled):normalize_pptx(source,self.root/'副本.pptx',cancel)

    def test_grouped_header_is_normalized(self):
        source=self.fixture([['基本信息','',''],['辅料编号','颜色','数量'],['001','黑','2']])
        self.assertTrue(normalize_pptx(source,self.root/'副本.pptx',threading.Event()))


if __name__=='__main__':unittest.main()
