"""Query exact material supplier identifiers using an authenticated OA transport."""
from copy import deepcopy
from urllib.parse import urlparse


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
