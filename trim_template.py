"""Render OA trim tables into the first sheet of an uploaded XLSX template."""

from __future__ import annotations

from copy import copy
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


def _match_columns(sheet, records: list[dict], generated_headers=()) -> tuple[int, dict[int, str]]:
    names = list(dict.fromkeys(name for record in records for name in record))
    normalized = {_key(name): name for name in names if _key(name)}
    generated = {_key(name) for name in generated_headers}
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
            if not label or label in generated:
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
                 if sheet.cell(best_row, column).value not in (None, "") and column not in best_map
                 and _key(sheet.cell(best_row, column).value) not in generated]
    if unmatched:
        raise ValueError(f"模板列无法自动匹配：{'、'.join(unmatched[:12])}。请补充列名映射。")
    return best_row, best_map



def export_trim_xlsx(template_path: Path, tables: list, destination: Path,
                     shared_fields: dict | None = None, *, supplier_lookup=None,
                     material_header: str = "辅料编号") -> dict:
    """Fill the first template sheet and save a separate, editable workbook."""
    from openpyxl import load_workbook

    records = _records(tables, shared_fields)
    if not records:
        raise ValueError("OA 打印页的辅料信息表没有可转换的数据。")
    workbook = load_workbook(template_path)
    temporary = destination.with_suffix(".tmp.xlsx")
    try:
        sheet = workbook.worksheets[0]
        # Legacy print templates may retain supplier columns after lookup is
        # disabled. Reserve them instead of requiring a source-field match.
        generated_headers = ("供应商", "供应商1", "供应商2", "供应商名称",
                             "供应商名称1", "供应商名称2", "Supplier", "Supplier1", "Supplier2")
        header_row, mapping = _match_columns(sheet, records, generated_headers)
        if supplier_lookup is None:
            from openpyxl.utils import get_column_letter

            supplier_keys = {_key(header) for header in generated_headers}
            for cell in sheet[header_row]:
                if _key(cell.value) in supplier_keys:
                    sheet.column_dimensions[get_column_letter(cell.column)].hidden = True
        data_row = header_row + 1
        supplier_values = []
        supplier_missing = 0
        if supplier_lookup is not None:
            # Lookup uses source records, independently of the customer's output columns.
            source_fields = list(dict.fromkeys(field for record in records for field in record))
            code_fields = [field for field in source_fields if _key(field) == _key(material_header)]
            if not code_fields:
                aliases = {_key(value) for value in ("辅料编号", "Code(编号)", "编号(Code)", "Material Code", "辅料编号 Trim Code")}
                code_fields = [field for field in source_fields if _key(field) in aliases]
            if not code_fields:
                code_fields = list(dict.fromkeys(field for col, field in mapping.items()
                    if _key(sheet.cell(header_row, col).value) == _key(material_header)))
            if not code_fields:
                raise ValueError(f"源辅料数据缺少编号字段，实际字段：{'、'.join(source_fields)}")
            cache = {}
            for record in records:
                codes = {str(record.get(field, "")).strip() for field in code_fields
                         if str(record.get(field, "")).strip()}
                if len(codes) > 1:
                    raise ValueError("同一行辅料的编号字段存在冲突，无法安全查询供应商。")
                code = next(iter(codes), "")
                if not code:
                    supplier_values.append(("", ""))
                    continue
                if code not in cache:
                    pair = supplier_lookup(code)
                    if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                            or any(not isinstance(value, str) for value in pair)):
                        raise ValueError(f"辅料 {code} 的供应商查询返回格式无效。")
                    cache[code] = tuple(pair)
                pair = cache[code]
                supplier_values.append(pair)
                if not any(pair):
                    supplier_missing += 1
        if any(r.min_row <= data_row <= r.max_row for r in sheet.merged_cells.ranges):
            raise ValueError("模板数据起始行包含合并单元格，无法安全自动填充。")
        if any(sheet.cell(data_row, col).value not in (None, "") for col in range(1, sheet.max_column + 1)):
            raise ValueError("模板表头下一行已有数据，请提供空白数据行作为填充位置。")

        # Reuse blank template rows; insert only if records would overwrite a footer.
        end_row = data_row + len(records) - 1
        occupied = [cell.row for row in sheet.iter_rows(min_row=data_row + 1, max_row=max(sheet.max_row, data_row))
                    for cell in row if cell.value is not None]
        occupied += [r.min_row for r in sheet.merged_cells.ranges if r.min_row > data_row]
        footer_row = min(occupied, default=end_row + 1)
        extra = max(0, end_row - footer_row + 1)
        if extra:
            # openpyxl does not update dependent formula references on row insertion.
            if any(cell.data_type == "f" for tab in workbook for row in tab for cell in row):
                raise ValueError("模板含公式且空白数据行不足，请在模板中预留更多空白行后重试。")
            merges = [copy(r) for r in sheet.merged_cells.ranges if r.min_row >= footer_row]
            for merged in merges:
                sheet.unmerge_cells(str(merged))
            dimensions = {index: copy(dim) for index, dim in sheet.row_dimensions.items() if index >= footer_row}
            for index in dimensions:
                del sheet.row_dimensions[index]
            sheet.insert_rows(footer_row, extra)
            for index, dim in dimensions.items():
                dim.index = index + extra
                sheet.row_dimensions[index + extra] = dim
            for merged in merges:
                merged.shift(row_shift=extra)
                sheet.merge_cells(str(merged))

        styles = {col: copy(sheet.cell(data_row, col)._style) for col in range(1, sheet.max_column + 1)}
        height = sheet.row_dimensions[data_row].height
        for offset, record in enumerate(records):
            row = data_row + offset
            if height is not None:
                sheet.row_dimensions[row].height = height
            for col, style in styles.items():
                sheet.cell(row, col)._style = copy(style)
            for col, field in mapping.items():
                cell = sheet.cell(row, col)
                value = offset + 1 if field == "__index__" else record.get(field, "")
                cell.value = value
                if isinstance(value, str):
                    # OA values are text, including codes with leading zeros or '='.
                    cell.data_type = "s"
        supplier_columns = []
        if supplier_lookup is not None:
            headers = {_key(sheet.cell(header_row, col).value): col
                       for col in range(1, sheet.max_column + 1)
                       if sheet.cell(header_row, col).value not in (None, "")}
            style_column = max(mapping)
            for index, title in enumerate(("供应商", "供应商2")):
                existing = headers.get(_key(title))
                if index == 0:
                    existing = existing or headers.get(_key("供应商1"))
                if index == 1 and not existing and not any(pair[1] for pair in supplier_values):
                    continue
                column = existing or sheet.max_column + 1
                supplier_columns.append(column)
                header = sheet.cell(header_row, column)
                if not existing:
                    header._style = copy(sheet.cell(header_row, style_column)._style)
                    from openpyxl.utils import get_column_letter
                    sheet.column_dimensions[get_column_letter(column)].width = 28
                header.value = title
                for offset, pair in enumerate(supplier_values):
                    cell = sheet.cell(data_row + offset, column)
                    if not existing:
                        cell._style = copy(sheet.cell(data_row + offset, style_column)._style)
                    cell.value = pair[index]
                    cell.data_type = "s"
            if sheet.print_area:
                from openpyxl.worksheet.cell_range import CellRange
                from openpyxl.worksheet.print_settings import PrintArea
                sheet.print_area = [str(CellRange(min_col=area.min_col, min_row=area.min_row,
                    max_col=max(area.max_col, max(supplier_columns)),
                    max_row=max(area.max_row, end_row)))
                    for area in PrintArea.from_string(sheet.print_area).ranges]
        workbook.save(temporary)
        os.replace(temporary, destination)
        return {"rows": len(records), "columns": len(mapping) + len(supplier_columns),
                "sheet": sheet.title, "supplier_missing": supplier_missing}
    finally:
        workbook.close()
        if temporary.exists():
            temporary.unlink()
