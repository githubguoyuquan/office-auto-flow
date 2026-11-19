"""Bounded text-table previews; no extraction or edits to the source file."""
from dataclasses import asdict
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

from table_recognition import recognize

ALIASES = {
    '编号':['Code(编号)','编号(Code)','Material Code'],
    '辅料编号':['辅料编码','Trim Code'], '面料编号':['面料编码','Fabric Code'],
    '名称':['Description(描述)','Description','品名'],
    '颜色':['Color','Colour','色号'], '用量':['Consumption(单耗)','单耗','用量/QTY'],
    '尺寸':['Size(使用尺寸)','Size','规格'], '使用部位':['Location(使用部位)','Location'],
    '备注':['Memo(备注)','Memo'], '正式款号':['Style#','Style No','款号'],
    '供应商':['Supplier1','供应商1','Vendor'], '供应商2':['Supplier2'],
}
NS = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
ROWS, COLS, TABLES = 200, 80, 20


def _report(rows, merges, location, warnings):
    result = recognize(rows, ALIASES, merges)
    candidates = []
    for candidate in result['candidates'][:10]:
        value = asdict(candidate)
        value['record_count_in_sample'] = len(value['records'])
        value['records'] = value['records'][:5]
        value['sources'] = value['sources'][:5]
        candidates.append(value)
    return {'location':location, 'sample_rows':len(rows),
            'sample_columns':max(map(len,rows),default=0),
            'status':'review' if warnings and result['status']=='ready' else result['status'],
            'warnings':warnings, 'candidates':candidates,
            'candidate_count':len(result['candidates'])}


def preview_tables(filename):
    path = Path(filename)
    if path.suffix.lower() not in {'.xlsx','.pptx'} or not path.is_file():
        raise ValueError('请选择可读取的 XLSX 或 PPTX 文件。')
    reports, warnings = [], []
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries)>10000 or sum(item.file_size for item in entries)>32*1024*1024:
                raise ValueError('文件解压后过大，请拆分后预览。')
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError('暂不支持加密文件。')
            if path.suffix.lower()=='.pptx':
                slides = sorted((name for name in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml',name)), key=lambda name:int(re.search(r'slide(\d+)',name)[1]))
                for name in slides:
                    raw = archive.read(name)
                    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
                        raise ValueError('文件包含不支持的 XML 声明。')
                    root = ET.fromstring(raw)
                    for number, table in enumerate(root.iter(NS+'tbl'),1):
                        if len(reports)>=TABLES:
                            warnings.append('仅预览前 20 个表区。');break
                        all_rows = table.findall(NS+'tr')
                        rows, merges, notes = [], [], []
                        for r,row in enumerate(all_rows[:ROWS]):
                            cells = row.findall(NS+'tc')
                            values=[]
                            for c,cell in enumerate(cells[:COLS]):
                                values.append('\n'.join(''.join(t.text or '' for t in para.iter(NS+'t')) for para in cell.findall('.//'+NS+'p')))
                                if cell.get('hMerge') in {'1','true'} or cell.get('vMerge') in {'1','true'}:
                                    values[-1]='';continue
                                rs,cs=int(cell.get('rowSpan','1')),int(cell.get('gridSpan','1'))
                                if rs<1 or cs<1:
                                    raise ValueError('PPTX 合并范围无效。')
                                if rs>1 or cs>1:
                                    merges.append((r,c,r+rs-1,c+cs-1))
                            rows.append(values)
                        width=max(map(len,rows),default=0)
                        valid=[m for m in merges if m[2]<len(rows) and m[3]<width]
                        if len(valid)!=len(merges) or len(all_rows)>ROWS or any(len(row.findall(NS+'tc'))>COLS for row in all_rows):
                            notes.append('表格超过预览范围或合并范围跨越边界，仅展示局部。')
                        reports.append(_report(rows,valid,f'{Path(name).stem} · 表 {number}',notes))
                    if len(reports)>=TABLES:break
            else:
                # Check XML declarations before passing the archive to openpyxl.
                for item in entries:
                    if item.filename.endswith('.xml'):
                        raw=archive.read(item)
                        if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
                            raise ValueError('文件包含不支持的 XML 声明。')
        if path.suffix.lower()=='.xlsx':
            from openpyxl import load_workbook
            book=load_workbook(path,data_only=False,read_only=False)
            try:
                for sheet in book.worksheets[:TABLES]:
                    notes=[]
                    rows=[[cell.value for cell in row] for row in sheet.iter_rows(max_row=min(sheet.max_row,ROWS),max_col=min(sheet.max_column,COLS))]
                    if not any(value is not None for row in rows for value in row):continue
                    merges=[(m.min_row-1,m.min_col-1,m.max_row-1,m.max_col-1) for m in sheet.merged_cells.ranges if m.max_row<=len(rows) and m.max_col<=len(rows[0])]
                    if sheet.max_row>ROWS or sheet.max_column>COLS:notes.append('仅预览前 200 行、80 列。')
                    if sheet.sheet_state!='visible':notes.append('此工作表在源文件中被隐藏。')
                    if any(isinstance(value,str) and value.startswith('=') for row in rows for value in row):notes.append('包含公式，预览显示公式原文，不计算公式。')
                    reports.append(_report(rows,merges,sheet.title,notes))
                if len(book.worksheets)>TABLES:warnings.append('仅预览前 20 个工作表。')
            finally:book.close()
    except (BadZipFile,ET.ParseError) as error:
        raise ValueError('文件损坏或格式无效。') from error
    if not reports:warnings.append('没有读取到文字表格；图片和扫描表格尚不支持。')
    return {'filename':path.name,'tables':reports,'warnings':warnings,
            'notice':'预览用于核对；正式 PPTX 提取已使用同一识别核心。多候选或复杂分区需先拆分确认。'}
