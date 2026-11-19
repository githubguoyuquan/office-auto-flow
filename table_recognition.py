"""Conservative, format-independent table structure candidates with provenance.

Adapters provide text cells and 0-based inclusive merge rectangles. Ambiguous
results require review; this module never chooses a layout silently.
"""
from dataclasses import dataclass
import re
import unicodedata


def field_key(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


@dataclass(frozen=True)
class Candidate:
    orientation: str
    header_start: int
    header_end: int
    fields: tuple
    records: tuple
    sources: tuple
    issues: tuple


def recognize(cells, aliases, merges=(), *, max_header_depth=3):
    """Return candidates and review status; aliases map canonical names to labels.

    No fuzzy matches, data fill-down or conversion of formula/image values.
    Merge anchors propagate only inside candidate headers, never into records.
    """
    if not isinstance(cells, (list, tuple)) or len(cells) > 10000:
        raise ValueError("表格行数无效或超过 10000 行。")
    if any(not isinstance(row, (list, tuple)) for row in cells):
        raise ValueError("表格必须为二维单元格数组。")
    width = max(map(len, cells), default=0)
    if width > 1000 or len(cells) * width > 500000:
        raise ValueError("表格过大，请分区识别。")
    if not 1 <= max_header_depth <= 5:
        raise ValueError("表头深度应为 1 至 5。")
    grid = [[str(v) if v is not None else '' for v in row] + ['']*(width-len(row)) for row in cells]
    keys = {}
    for canonical, names in aliases.items():
        for name in [canonical, *names]:
            key = field_key(name)
            if not key:
                continue
            if key in keys and keys[key] != canonical:
                raise ValueError(f"字段别名冲突：{name}")
            keys[key] = canonical
    anchors = {}
    for r0, c0, r1, c1 in merges:
        if not (0 <= r0 <= r1 < len(grid) and 0 <= c0 <= c1 < width):
            raise ValueError("合并单元格范围无效。")
        for r in range(r0, r1+1):
            for c in range(c0, c1+1):
                if (r, c) in anchors:
                    raise ValueError("合并单元格范围重叠。")
                anchors[r, c] = (r0, c0)
    candidates = []
    for orientation in ('rows', 'columns'):
        transpose = orientation == 'columns'
        height, span = (width, len(grid)) if transpose else (len(grid), width)
        def coord(r, c):
            return (c, r) if transpose else (r, c)
        def read(r, c, header=False):
            source = coord(r, c)
            if header:
                source = anchors.get(source, source)
            return grid[source[0]][source[1]]
        for start in range(min(height, 40)):
            for depth in range(1, min(max_header_depth, height-start)+1):
                # Do not swallow data rows into an increasingly deep header.
                last = start + depth - 1
                if not any(keys.get(field_key(read(last,c,True))) or keys.get(field_key(' '.join(
                        read(r,c,True) for r in range(start,last+1)))) for c in range(span)):
                    continue
                fields, issues = [], []
                for c in range(span):
                    parts = list(dict.fromkeys(read(r, c, True).strip() for r in range(start,start+depth) if read(r,c,True).strip()))
                    matches = {keys[field_key(label)] for label in parts+[' '.join(parts)] if field_key(label) in keys}
                    if len(matches) > 1:
                        issues.append(f"第 {c+1} 项存在多个字段解释")
                    fields.append(next(iter(matches)) if len(matches)==1 else None)
                recognized = [field for field in fields if field]
                if len(set(recognized)) < 2:
                    continue
                if len(set(recognized)) != len(recognized):
                    issues.append('字段重复，可能包含并列表或多维分组')
                records, sources = [], []
                for r in range(start+depth, height):
                    if not any(read(r,c).strip() for c in range(span)):
                        break
                    if any(keys.get(field_key(read(r,c))) == field for c,field in enumerate(fields) if field):
                        issues.append('检测到重复表头或后续表区，需要分区确认')
                        break
                    records.append(tuple(read(r,c) for c,field in enumerate(fields) if field))
                    sources.append(tuple(coord(r,c) for c,field in enumerate(fields) if field))
                if records:
                    candidates.append(Candidate(orientation,start,start+depth-1,tuple(fields),tuple(records),tuple(sources),tuple(dict.fromkeys(issues))))
    # Equivalent header windows share the same fields/data provenance.
    unique = {}
    for item in candidates:
        key = (item.orientation,item.fields,item.sources)
        if key not in unique or item.header_end-item.header_start < unique[key].header_end-unique[key].header_start:
            unique[key] = item
    ranked = sorted(unique.values(),key=lambda item:(-len(set(f for f in item.fields if f)),len(item.issues),item.header_end-item.header_start))
    safe = len(ranked)==1 and not ranked[0].issues
    return {'status':'ready' if safe else 'review' if ranked else 'unrecognized', 'candidates':ranked}
