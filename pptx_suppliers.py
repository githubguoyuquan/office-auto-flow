"""Fill template supplier columns without exposing intermediate lookup columns."""
from pathlib import Path
import re
import tempfile
import unicodedata

from pptx_service import ExtractionCancelled, invoke, publish_output


def supplier_columns(headers):
    result = {}
    for index, header in enumerate(headers, 1):
        for name in re.split(r"[|｜]", str(header or "")):
            key = "".join(unicodedata.normalize("NFKC", name).split()).casefold()
            if key in {"供应商", "供应商名称", "supplier", "suppliername"}:
                result[index] = -1
                break
            if key in {"供应商1", "供应商一", "供应商名称1", "supplier1", "suppliername1"}:
                result[index] = 0
                break
            if key in {"供应商2", "供应商二", "供应商名称2", "supplier2", "suppliername2"}:
                result[index] = 1
                break
    return result


def material_codes(value):
    if value is None:
        return []
    return list(dict.fromkeys(code.strip() for code in str(value).split(" / ") if code.strip()))


def has_materials(path, code_column):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True)
    try:
        return any(material_codes(row[0]) for row in book.active.iter_rows(
            min_row=2, min_col=code_column, max_col=code_column, values_only=True))
    finally:
        book.close()


def fill_suppliers(path, code_column, columns, lookup, cancel):
    from openpyxl import load_workbook
    book = load_workbook(path)
    cache, missing = {}, 0
    try:
        sheet = book.worksheets[0]
        for row in range(2, sheet.max_row + 1):
            if cancel.is_set():
                raise ExtractionCancelled()
            codes = material_codes(sheet.cell(row, code_column).value)
            pairs = []
            for code in codes:
                if code not in cache:
                    pair = lookup(code)
                    if not isinstance(pair, (tuple, list)) or len(pair) != 2 or any(not isinstance(value, str) for value in pair):
                        raise ValueError(f"辅料 {code} 的供应商响应格式异常。")
                    cache[code] = pair
                pairs.append(cache[code])
            if codes and not any(any(pair) for pair in pairs):
                missing += 1
            for column, slot in columns.items():
                values = [value for pair in pairs for value in (pair if slot == -1 else [pair[slot]]) if value]
                cell = sheet.cell(row, column, " / ".join(dict.fromkeys(values)))
                cell.data_type = "s"
        sheet.delete_cols(code_column)
        if sheet.auto_filter.ref:
            from openpyxl.utils import get_column_letter
            sheet.auto_filter.ref = f"A1:{get_column_letter(code_column - 1)}{sheet.max_row}"
        if cancel.is_set():
            raise ExtractionCancelled()
        book.save(path)
        return {"supplier_queries": len(cache), "supplier_missing": missing}
    finally:
        book.close()


def extract_with_suppliers(request, cancel, on_event, binary, run_supplier_stage, *, enabled=True):
    analysis = invoke({"action": "analyze_template", "template": request["template"],
                       "rules": request.get("rules", [])}, cancel, lambda event: None, binary=binary, timeout=60)
    columns = supplier_columns(analysis["headers"]) if enabled else {}
    if not columns:
        return invoke(request, cancel, on_event, binary=binary)
    if len(analysis["headers"]) >= 16384:
        raise ValueError("模板列数过多，无法读取供应商查询所需的辅料编号。")
    destination = Path(request.get("output_dir") or Path(request["inputs"][0]).parent)
    with tempfile.TemporaryDirectory(prefix=".pptx-suppliers-", dir=destination) as temporary:
        root = Path(temporary)
        from openpyxl import load_workbook
        book = load_workbook(request["template"])
        code_column = len(analysis["headers"]) + 1
        try:
            book.active.cell(1, code_column, "__OAF_LOOKUP_MATERIAL__")
            template = root / "lookup-template.xlsx"
            book.save(template)
        finally:
            book.close()
        mapping = request.get("mapping")
        if mapping is None:
            mapping = analysis["mapping"]
        if len(mapping) != len(analysis["headers"]):
            raise ValueError("模板列匹配已变化，请重新选择模板。")
        request = dict(request, template=str(template), output_dir=str(root),
                       mapping=[*mapping, analysis["fields"].index("辅料编号")])
        result = invoke(request, cancel, on_event, binary=binary)
        source = Path(result["output"])
        if has_materials(source, code_column):
            statistics = run_supplier_stage(source, code_column, columns)
        else:
            statistics = fill_suppliers(source, code_column, columns, lambda code: ("", ""), cancel)
        if cancel.is_set():
            raise ExtractionCancelled()
        output = publish_output(source, destination, cancel)
        return dict(result, output=str(output), **statistics)
