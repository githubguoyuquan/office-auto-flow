"""Normalize confidently identified PPTX material tables in disposable copies."""
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, is_zipfile
import re
import shutil

from table_recognition import recognize, field_key
from table_sources import ALIASES, NS

MAX_SLIDE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTION_BYTES = 128 * 1024 * 1024


def mixed_orientation_sections(rows, aliases):
    width = max(map(len, rows), default=0)
    grid = [row + [''] * (width - len(row)) for row in rows]
    layouts = []
    for transposed in (False, True):
        lines = list(zip(*grid)) if transposed else grid
        blocks, current = [], []
        for line in lines:
            if any(value.strip() for value in line):
                current.append(line)
            elif current:
                blocks.append(current)
                current = []
        if current:
            blocks.append(current)
        if len(blocks) < 2:
            continue
        sections = [list(map(list, zip(*block))) if transposed else block for block in blocks]
        orientations = set()
        for section in sections:
            candidate = material_candidates(section, aliases, [])
            if len(candidate) != 1 or candidate[0].issues:
                break
            orientations.add(candidate[0].orientation)
        else:
            if len(orientations) == 2:
                layouts.append(sections)
    return layouts[0] if len(layouts) == 1 else []


def material_candidates(rows, aliases, merges):
    candidates = [
        candidate for candidate in recognize(rows, aliases, merges)['candidates']
        if {'面料编号', '辅料编号'}.intersection(candidate.fields)
    ]
    counts = [len(set(candidate.fields) - {None}) for candidate in candidates]
    best = max(counts, default=0)
    return [candidate for candidate, count in zip(candidates, counts) if count == best]


def write_rows(table, rows):
    for values in rows:
        row = ET.SubElement(table, NS + 'tr', {'h': '1'})
        for value in values:
            cell = ET.SubElement(row, NS + 'tc')
            body = ET.SubElement(cell, NS + 'txBody')
            paragraph = ET.SubElement(body, NS + 'p')
            run = ET.SubElement(paragraph, NS + 'r')
            ET.SubElement(run, NS + 't').text = value


def merge_repeated_sections(rows, candidates):
    """Remove identical header blocks and duplicate records from a plain grid."""
    width = max(map(len, rows))
    grid = [tuple(value.strip() for value in row) + ('',) * (width - len(row)) for row in rows]
    layouts = set()
    for candidate in candidates:
        transposed = candidate.orientation == 'columns'
        lines = list(zip(*grid)) if transposed else grid
        start, end = candidate.header_start, candidate.header_end + 1
        header = lines[start:end]
        if not header or not any(any(line) for line in header):
            continue
        merged = lines[:end]
        seen = set()
        repeated = False
        index = end
        while index < len(lines):
            if lines[index:index + len(header)] == header:
                repeated = True
                index += len(header)
                continue
            line = lines[index]
            if any(line) and line not in seen:
                merged.append(line)
                seen.add(line)
            index += 1
        if repeated:
            layouts.add(tuple(zip(*merged)) if transposed else tuple(merged))
    if len(layouts) == 1:
        return [list(row) for row in layouts.pop()]
    return None


def normalize_pptx(source, destination, cancel):
    # Let the existing extractor report individual missing/corrupt input files.
    if not Path(source).is_file() or not is_zipfile(source):return False
    aliases={**ALIASES}
    aliases['颜色']=[*aliases['颜色'],'面料颜色','辅料颜色']
    changed=0
    with ZipFile(source) as archive:
        # The extraction component only reads slide text and its index.
        # Do not decompress media into the temporary extraction input.
        entries = [entry for entry in archive.infolist() if not entry.filename.startswith('ppt/media/')]
        if len(entries) > 10000 or sum(entry.file_size for entry in entries) > MAX_EXTRACTION_BYTES:
            raise ValueError(f'{Path(source).name}：PPTX 除图片等媒体素材外的内容过大，请拆分后提取。')
        replacements={}
        for entry in entries:
            if cancel.is_set():
                from pptx_service import ExtractionCancelled
                raise ExtractionCancelled()
            if not re.fullmatch(r'ppt/slides/slide\d+\.xml',entry.filename):continue
            if entry.file_size > MAX_SLIDE_BYTES:
                raise ValueError(f'{Path(source).name} · {Path(entry.filename).stem}：单页内容超过 32 MB，请精简该页后重试。')
            raw=archive.read(entry)
            if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise ValueError('不支持的 XML 声明。')
            root=ET.fromstring(raw);dirty=False
            parents = {child: parent for parent in root.iter() for child in parent if child.tag == NS + 'tbl'}
            tables = [(table, True) for table in root.iter(NS + 'tbl')]
            for number,(table,split_allowed) in enumerate(tables,1):
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
                if split_allowed and not merges:
                    sections = mixed_orientation_sections(rows, aliases)
                    if sections and table in parents:
                        parent = parents[table]
                        position = list(parent).index(table)
                        parent.remove(table)
                        for offset, section in enumerate(sections):
                            part = ET.Element(NS + 'tbl')
                            write_rows(part, section)
                            parent.insert(position + offset, part)
                            tables.append((part, False))
                        dirty = True
                        continue
                candidates = material_candidates(rows, aliases, merges)
                if not candidates:continue  # Preserve legacy inline/label layouts.
                count = len(set(candidates[0].fields) - {None})
                if not merges and (len(candidates) != 1 or candidates[0].issues):
                    merged = merge_repeated_sections(rows, candidates)
                    if merged is not None:
                        resolved = [
                            item for item in recognize(merged, aliases)['candidates']
                            if any(field in {'面料编号', '辅料编号'} for field in item.fields)
                            and len(set(item.fields) - {None}) == count
                        ]
                        if len(resolved) == 1 and not resolved[0].issues:
                            rows, candidates = merged, resolved
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
                write_rows(table, normalized)
                dirty=True;changed+=1
            if dirty:replacements[entry.filename]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
        if not changed:return False
        with ZipFile(destination,'w',ZIP_DEFLATED) as output:
            for entry in entries:
                if cancel.is_set():
                    from pptx_service import ExtractionCancelled
                    raise ExtractionCancelled()
                if entry.filename in replacements:
                    output.writestr(entry.filename, replacements[entry.filename])
                else:
                    with archive.open(entry) as member, output.open(entry.filename, 'w') as target:
                        shutil.copyfileobj(member, target)
    return True
