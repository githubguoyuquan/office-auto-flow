"""Render OA trim tables into the first sheet of an uploaded XLSX template."""

from __future__ import annotations

import html
import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


def _key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]", "", normalized)


def _records(tables: list, shared_fields: dict | None = None) -> list[dict]:
    """Transpose each source material column to a row, then add sheet-wide fields."""
    records = []
    for table in tables:
        width = max((len(row) - 1 for row in table), default=0)
        for index in range(width):
            record = {}
            for row in table:
                if row and len(row) > index + 1 and str(row[0]).strip():
                    record[str(row[0]).strip()] = str(row[index + 1]).strip()
            if any(record.values()):
                record.update(shared_fields or {})
                records.append(record)
    return records


def _match_columns(sheet, records: list[dict]) -> tuple[int, dict[int, str]]:
    names = list(dict.fromkeys(name for record in records for name in record))
    normalized = {_key(name): name for name in names if _key(name)}
    if "正式款号" in names:
        for alias in ("款号", "Style#", "Style No", "Style Number", "Official Style Code",
                      "正式款号 Style#", "正式款号 Style No"):
            normalized.setdefault(_key(alias), "正式款号")
    best_row, best_map = 0, {}
    for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 40)):
        mapping = {}
        used = set()
        for cell in row:
            label = _key(cell.value)
            if not label:
                continue
            source = normalized.get(label)
            if source is None and label in {"序号", "no", "number", "index"}:
                source = "__index__"
            if source is None and len(label) >= 4:
                candidates = sorted(
                    ((SequenceMatcher(None, label, key).ratio(), name)
                     for key, name in normalized.items() if len(key) >= 4),
                    reverse=True,
                )
                if (candidates and candidates[0][0] >= 0.85
                        and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 0.05)):
                    source = candidates[0][1]
            if source and source not in used:
                mapping[cell.column] = source
                used.add(source)
        if len(mapping) > len(best_map):
            best_row, best_map = row[0].row, mapping
    if len(best_map) < 2:
        available = "、".join(names[:12])
        raise ValueError(f"模板首个工作表未找到至少两列可对应的辅料表头。OA 字段：{available}")
    unmatched = [str(sheet.cell(best_row, column).value) for column in range(1, sheet.max_column + 1)
                 if sheet.cell(best_row, column).value not in (None, "") and column not in best_map]
    if unmatched:
        raise ValueError(f"模板列无法自动匹配：{'、'.join(unmatched[:12])}。请补充列名映射。")
    return best_row, best_map


def _rgb(color) -> str | None:
    if color is not None and color.type == "rgb" and isinstance(color.rgb, str):
        return "#" + color.rgb[-6:]
    return None


def _cell_css(cell) -> str:
    parts = ["border:1px solid #999", "padding:4px 6px", "white-space:pre-wrap"]
    if cell.font.bold:
        parts.append("font-weight:700")
    if cell.font.sz:
        parts.append(f"font-size:{cell.font.sz}pt")
    foreground = _rgb(cell.font.color)
    if foreground:
        parts.append(f"color:{foreground}")
    background = _rgb(cell.fill.fgColor) if cell.fill.patternType == "solid" else None
    if background:
        parts.append(f"background:{background}")
    if cell.alignment.horizontal in {"left", "center", "right"}:
        parts.append(f"text-align:{cell.alignment.horizontal}")
    if cell.alignment.vertical in {"top", "center", "bottom"}:
        parts.append(f"vertical-align:{'middle' if cell.alignment.vertical == 'center' else cell.alignment.vertical}")
    return ";".join(parts)


def _render_html(sheet, records: list[dict], header_row: int, mapping: dict[int, str]) -> str:
    data_row = header_row + 1
    if any(range_.min_row <= data_row <= range_.max_row for range_ in sheet.merged_cells.ranges):
        raise ValueError("模板数据起始行包含合并单元格，无法安全自动填充。")
    if any(sheet.cell(data_row, column).value not in (None, "") for column in mapping):
        raise ValueError("模板表头下一行已有数据，请提供空白数据行作为填充位置。")
    if sheet._images or sheet._charts:
        raise ValueError("模板包含图片或图表，当前无法保证 PDF 版式准确。")

    extra = len(records) - 1
    spans = {}
    skipped = set()
    for merged in sheet.merged_cells.ranges:
        start = merged.min_row + (extra if merged.min_row > data_row else 0)
        end = merged.max_row + (extra if merged.max_row > data_row else 0)
        spans[(start, merged.min_col)] = (end - start + 1, merged.max_col - merged.min_col + 1)
        for row in range(start, end + 1):
            for col in range(merged.min_col, merged.max_col + 1):
                if (row, col) != (start, merged.min_col):
                    skipped.add((row, col))

    max_column = min(max(sheet.max_column, max(mapping)), 60)
    if sheet.max_row > 200 or max_column > 50:
        raise ValueError("模板首个工作表过大，请提供只包含目标版式的模板。")
    widths = []
    from openpyxl.utils import get_column_letter

    for col in range(1, max_column + 1):
        width = sheet.column_dimensions[get_column_letter(col)].width or 12
        widths.append(f'<col style="width:{max(5, min(width, 50))}ch">')

    rows_html = []
    for output_row in range(1, max(sheet.max_row, data_row) + extra + 1):
        if data_row <= output_row < data_row + len(records):
            source_row = data_row
            record = dict(records[output_row - data_row], __index__=output_row - data_row + 1)
        else:
            source_row = output_row - extra if output_row >= data_row + len(records) else output_row
            record = None
        cells = []
        for col in range(1, max_column + 1):
            if (output_row, col) in skipped:
                continue
            cell = sheet.cell(source_row, col)
            value = record.get(mapping[col], "") if record is not None and col in mapping else cell.value
            text = html.escape(str(value if value is not None else "")).replace("\n", "<br>")
            rowspan, colspan = spans.get((output_row, col), (1, 1))
            attrs = (f' rowspan="{rowspan}"' if rowspan > 1 else "") + (f' colspan="{colspan}"' if colspan > 1 else "")
            cells.append(f'<td{attrs} style="{_cell_css(cell)}">{text}</td>')
        height = sheet.row_dimensions[source_row].height
        style = f' style="height:{height}pt"' if height else ""
        rows_html.append(f'<tr{style}>{"".join(cells)}</tr>')

    landscape = sheet.page_setup.orientation == "landscape"
    orientation = "landscape" if landscape else "portrait"
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>'
            f'@page{{size:A4 {orientation};margin:10mm}}body{{font-family:"Microsoft YaHei",Arial,sans-serif}}'
            f'table{{border-collapse:collapse;width:100%;table-layout:fixed}}tr{{break-inside:avoid}}'
            f'</style></head><body><table><colgroup>{"".join(widths)}</colgroup>'
            f'{"".join(rows_html)}</table></body></html>')


def render_trim_pdf(context, template_path: Path, tables: list, destination: Path,
                    shared_fields: dict | None = None) -> dict:
    from openpyxl import load_workbook

    records = _records(tables, shared_fields)
    if not records:
        raise ValueError("OA 打印页的辅料信息表没有可转换的数据。")
    workbook = load_workbook(template_path, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        header_row, mapping = _match_columns(sheet, records)
        content = _render_html(sheet, records, header_row, mapping)
        landscape = sheet.page_setup.orientation == "landscape"
        sheet_name = sheet.title
    finally:
        workbook.close()

    pdf_page = context.new_page()
    temporary = destination.with_suffix(".tmp.pdf")
    try:
        pdf_page.set_content(content, wait_until="domcontentloaded")
        pdf_page.pdf(path=str(temporary), format="A4", landscape=landscape,
                     prefer_css_page_size=True, print_background=True)
        os.replace(temporary, destination)
    finally:
        pdf_page.close()
        if temporary.exists():
            temporary.unlink()
    return {"rows": len(records), "columns": len(mapping), "sheet": sheet_name}
