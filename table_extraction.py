"""Normalize confidently identified PPTX material tables in disposable copies."""
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, is_zipfile
import re

from table_recognition import recognize, field_key
from table_sources import ALIASES, NS


def normalize_pptx(source, destination, cancel):
    # Let the existing extractor report individual missing/corrupt input files.
    if not Path(source).is_file() or not is_zipfile(source):return False
    aliases={**ALIASES}
    aliases['颜色']=[*aliases['颜色'],'面料颜色','辅料颜色']
    changed=0
    with ZipFile(source) as archive:
        entries=archive.infolist()
        if len(entries)>10000 or sum(item.file_size for item in entries)>32*1024*1024:
            raise ValueError('文件过大，请拆分后提取。')
        replacements={}
        for entry in entries:
            if cancel.is_set():
                from pptx_service import ExtractionCancelled
                raise ExtractionCancelled()
            if not re.fullmatch(r'ppt/slides/slide\d+\.xml',entry.filename):continue
            raw=archive.read(entry)
            if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise ValueError('不支持的 XML 声明。')
            root=ET.fromstring(raw);dirty=False
            for number,table in enumerate(root.iter(NS+'tbl'),1):
                rows=[];merges=[]
                for r,row in enumerate(table.findall(NS+'tr')):
                    values=[]
                    for c,cell in enumerate(row.findall(NS+'tc')):
                        value='\n'.join(''.join(t.text or '' for t in p.iter(NS+'t')) for p in cell.findall('.//'+NS+'p'))
                        if cell.get('hMerge') in {'1','true'} or cell.get('vMerge') in {'1','true'}:value=''
                        else:
                            rs,cs=int(cell.get('rowSpan','1')),int(cell.get('gridSpan','1'))
                            if rs>1 or cs>1:merges.append((r,c,r+rs-1,c+cs-1))
                        values.append(value)
                    rows.append(values)
                # Generic Code/编号 cannot safely identify fabric versus trim.
                material_keys={field_key(name) for name in ['面料编号','面料名称','面料','辅料编号','辅料编码','辅料','Fabric Code','Trim Code']}
                if not any(field_key(v) in material_keys for row in rows for v in row):continue
                result=recognize(rows,aliases,merges)
                candidates=[c for c in result['candidates'] if any(f in {'面料编号','辅料编号'} for f in c.fields)]
                if not candidates:continue  # Preserve legacy inline/label layouts.
                count=max(len(set(f for f in c.fields if f)) for c in candidates)
                candidates=[c for c in candidates if len(set(f for f in c.fields if f))==count]
                if len(candidates)!=1 or candidates[0].issues:
                    raise ValueError(f'{Path(source).name} · {Path(entry.filename).stem} 表 {number}：表格有多个结构解释或重复表区，请先在制作模板中预览并拆分表区。')
                candidate=candidates[0]
                transposed=candidate.orientation=='columns'
                if any((m[3] if transposed else m[2]) > candidate.header_end for m in merges):
                    raise ValueError(f'{Path(source).name} · {Path(entry.filename).stem} 表 {number}：数据区有合并单元格，请先拆分并确认各行对应值。')
                height,width=(max(map(len,rows)),len(rows)) if transposed else (len(rows),max(map(len,rows)))
                anchors={(r,c):(r0,c0) for r0,c0,r1,c1 in merges for r in range(r0,r1+1) for c in range(c0,c1+1)}
                def read(r,c,header=False):
                    rr,cc=(c,r) if transposed else (r,c)
                    if header:rr,cc=anchors.get((rr,cc),(rr,cc))
                    return rows[rr][cc] if cc<len(rows[rr]) else ''
                headers=[]
                for c,field in enumerate(candidate.fields):
                    parts=list(dict.fromkeys(read(r,c,True).strip() for r in range(candidate.header_start,candidate.header_end+1) if read(r,c,True).strip()))
                    # Retain user-defined/extra labels so template exact matching
                    # still works after structural normalization.
                    label = parts[-1] if parts else ''
                    if field in {'面料编号','辅料编号'}:label=field
                    elif field=='颜色' and label not in {'color','COLOR'}:label='COLOR'
                    elif field=='用量':label='数量'
                    headers.append(label)
                normalized=[headers]
                for r in range(candidate.header_end+1,height):
                    row=[read(r,c) for c in range(width)]
                    if any(v.strip() for v in row):normalized.append(row)
                if len(normalized)<2:continue
                for row in table.findall(NS+'tr'):table.remove(row)
                for values in normalized:
                    row=ET.SubElement(table,NS+'tr',{'h':'1'})
                    for value in values:
                        cell=ET.SubElement(row,NS+'tc');body=ET.SubElement(cell,NS+'txBody')
                        para=ET.SubElement(body,NS+'p');run=ET.SubElement(para,NS+'r');ET.SubElement(run,NS+'t').text=value
                dirty=True;changed+=1
            if dirty:replacements[entry.filename]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
        if not changed:return False
        with ZipFile(destination,'w',ZIP_DEFLATED) as output:
            for entry in entries:output.writestr(entry,replacements.get(entry.filename,archive.read(entry)))
    return True
