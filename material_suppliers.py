"""Query exact material supplier identifiers using an authenticated OA transport."""
from copy import deepcopy
import base64
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse

MAX_MAPPING_BYTES = 5 * 1024 * 1024


def read_supplier_mapping(filename, encoded):
    """Read code/name pairs from the first two columns without losing text codes."""
    if not isinstance(filename, str) or not filename.lower().endswith(".xlsx"):
        raise ValueError("供应商名称映射表必须是 XLSX 文件。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        raise ValueError("供应商名称映射表文件编码无效。")
    if not content or len(content) > MAX_MAPPING_BYTES:
        raise ValueError("供应商名称映射表不能超过 5 MB。")
    from openpyxl import load_workbook
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except Exception as error:
        raise ValueError("无法读取供应商名称映射表，请检查 XLSX 文件。") from error
    try:
        sheet = workbook.worksheets[0]
        if sheet.max_row and sheet.max_row > 50001:
            raise ValueError("供应商名称映射表最多支持 50000 条数据。")
        mapping = {}
        first = True
        for index, row in enumerate(sheet.iter_rows(max_col=2), 1):
            code_cell, name_cell = row
            if code_cell.data_type == "f" or name_cell.data_type == "f":
                raise ValueError(f"供应商映射表第 {index} 行包含公式，请改为编号和名称原值。")
            code, name = code_cell.value, name_cell.value
            if code in (None, "") and name in (None, ""):
                continue
            if code is None or (isinstance(code, str) and not code.strip()):
                raise ValueError(f"供应商映射表第 {index} 行缺少供应商编号，请补全后重新上传。")
            if type(code) in (int, float):
                if not math.isfinite(code) or code != int(code):
                    raise ValueError(f"供应商映射表第 {index} 行编号必须为文本或整数。")
                code = str(int(code))
                if re.fullmatch(r"0+", code_cell.number_format or ""):
                    code = code.zfill(len(code_cell.number_format))
            elif not isinstance(code, str):
                raise ValueError(f"供应商映射表第 {index} 行编号无效。")
            code = code.strip()
            if not isinstance(name, str) or not name.strip() or not code:
                raise ValueError(f"供应商映射表第 {index} 行缺少编号或供应商名称。")
            name = name.strip()
            if first and code in {"供应商编号", "供应商代码", "supplierCode"} and name in {"供应商名称", "supplierName"}:
                first = False
                continue
            first = False
            if code in mapping and mapping[code] != name:
                raise ValueError(f"供应商编号 {code} 对应多个不同名称，请先修正映射表。")
            mapping[code] = name
        if not mapping:
            raise ValueError("供应商名称映射表没有有效数据。")
        return mapping
    finally:
        workbook.close()


def supplier_names(codes, mapping):
    return tuple(mapping.get(code, code) if code else "" for code in codes)


def load_supplier_mapping(directory):
    path = Path(directory) / "supplier-names.json"
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {"entries": {}}
    except (ValueError, OSError) as error:
        raise ValueError("已保存的供应商映射表无法读取，请在应用配置中重新上传或清除。") from error
    entries = value.get("entries") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("version") != 1 or not isinstance(entries, dict)
            or not entries or len(entries) > 50000 or any(
                not isinstance(code, str) or not code.strip() or not isinstance(name, str) or not name.strip()
                for code, name in entries.items())
            or not isinstance(value.get("filename"), str) or not isinstance(value.get("uploaded_at"), str)):
        raise ValueError("已保存的供应商映射表格式无效，请在应用配置中重新上传或清除。")
    return value


def supplier_mapping_status(directory):
    try:
        value = load_supplier_mapping(directory)
    except ValueError as error:
        return {"configured": False, "count": 0, "error": str(error)}
    return {"configured": bool(value["entries"]), "count": len(value["entries"]),
            "filename": value.get("filename", ""), "uploaded_at": value.get("uploaded_at", "")}


def save_supplier_mapping(directory, filename, encoded):
    entries = read_supplier_mapping(filename, encoded)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    value = {"version": 1, "filename": filename.replace("\\", "/").rsplit("/", 1)[-1],
             "uploaded_at": datetime.now(timezone.utc).isoformat(), "entries": entries}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, directory / "supplier-names.json")
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return supplier_mapping_status(directory)


DEFAULT_SUPPLIER_LOOKUP = {
    "enabled": True,
    "endpoint": "material/queryMaterialListWithPage",
    "material_header": "辅料编号",
    "code_parameter": "materialCode",
    "response_code": "materialCode",
    "supplier_fields": ["supplier1", "supplier2"],
    "request_defaults": {
        "pageNum": 1, "pageSize": 10, "dataType": "Z200",
        "matnr": "", "maktx": "", "zzitemH1": "", "zzitemCd": "",
        "materialQuality": "", "supplementDesc": "", "managementType": "",
        "creator": "", "supplier": "", "supplierCode": "",
        "materialCode": "", "syncSapStatus": "",
    },
}


def validate_supplier_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SUPPLIER_LOOKUP):
        raise ValueError("供应商查询配置包含未知项目。")
    settings = {**deepcopy(DEFAULT_SUPPLIER_LOOKUP), **deepcopy(value)}
    if type(settings["enabled"]) is not bool:
        raise ValueError("供应商查询开关必须为 true 或 false。")
    for name in ("endpoint", "material_header", "code_parameter", "response_code"):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise ValueError(f"供应商查询配置 {name} 无效。")
    endpoint = urlparse(settings["endpoint"])
    if endpoint.scheme or endpoint.netloc or endpoint.query or endpoint.fragment:
        raise ValueError("供应商查询接口必须为同站点路径。")
    fields = settings["supplier_fields"]
    if (not isinstance(fields, list) or len(fields) != 2 or
            any(not isinstance(field, str) or not field.strip() for field in fields)
            or fields[0] == fields[1]):
        raise ValueError("供应商查询必须配置两个不同的供应商字段。")
    defaults = settings["request_defaults"]
    if not isinstance(defaults, dict) or any(
        not isinstance(key, str) or not key or type(item) not in (str, int, float, bool, type(None))
        for key, item in defaults.items()
    ):
        raise ValueError("供应商查询请求参数无效。")
    settings["request_defaults"] = {**DEFAULT_SUPPLIER_LOOKUP["request_defaults"], **defaults}
    size = settings["request_defaults"]["pageSize"]
    if type(size) is not int or not 1 <= size <= 1000:
        raise ValueError("供应商查询每页数量必须为 1 至 1000 的整数。")
    return settings


class MaterialSupplierLookup:
    def __init__(self, post, settings=None):
        self.post = post
        self.settings = validate_supplier_settings(settings or {})
        self.cache = {}

    def __call__(self, material_code):
        code = str(material_code).strip()
        if not code:
            return ("", "")
        if code in self.cache:
            return self.cache[code]
        pairs = set()
        settings = self.settings
        size = settings["request_defaults"]["pageSize"]
        for page in range(1, 201):
            request = dict(settings["request_defaults"], pageNum=page)
            request[settings["code_parameter"]] = code
            payload = self.post(settings["endpoint"], request)
            if not isinstance(payload, dict) or payload.get("code") != 10000:
                raise ValueError(f"辅料 {code} 的供应商查询接口返回错误。")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError(f"辅料 {code} 的供应商查询响应格式异常。")
            rows = data["list"]
            for row in rows:
                if not isinstance(row, dict) or settings["response_code"] not in row:
                    raise ValueError(f"供应商查询结果缺少辅料编号字段 {settings['response_code']}。")
                if str(row[settings["response_code"]]).strip() != code:
                    continue
                pair = []
                for field in settings["supplier_fields"]:
                    value = row.get(field)
                    if value is not None and type(value) not in (str, int):
                        raise ValueError(f"辅料 {code} 的 {field} 字段格式异常。")
                    pair.append("" if value is None else str(value).strip())
                pairs.add(tuple(pair))
            count = data.get("pageCount")
            if count is not None:
                if type(count) is int:
                    pages = count
                elif isinstance(count, str) and count.isdigit():
                    pages = int(count)
                else:
                    raise ValueError("供应商查询返回了无效的分页数量。")
                if pages < 0 or (pages == 0 and rows) or pages > 200:
                    raise ValueError("供应商查询分页数量异常或超过上限。")
                if page >= pages:
                    break
                if not rows:
                    raise ValueError("供应商查询分页提前返回空数据，已停止以避免遗漏。")
            elif len(rows) < size:
                break
        else:
            raise ValueError("供应商查询超过 200 页，已停止。")
        if len(pairs) > 1:
            raise ValueError(f"辅料 {code} 存在多条供应商不一致的记录，请核对 OA 数据。")
        result = next(iter(pairs), ("", ""))
        self.cache[code] = result
        return result
