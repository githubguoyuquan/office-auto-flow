"""Common OA protocol defaults; customer branches own customer-profile.json."""
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse
from material_suppliers import DEFAULT_SUPPLIER_LOOKUP, validate_supplier_settings

DEFAULTS = {
    "customer_id": "common",
    "business_url": "",
    "business_path": "/activiti/html/plm/styleManage/newStyleManage.html",
    "compatibility_hosts": [],
    "query_endpoint": "bomTitle/queryProcessSheet",
    "detail_endpoint": "bomTitle/printProcessSheet",
    "api_base_path": "/activiti/",
    "query_defaults": {"dataType": "Z400"},
    "supplier_lookup": DEFAULT_SUPPLIER_LOOKUP,
    "fields": {
        "query_style": "styleCode", "row_style": "styleCode",
        "row_id": "bomTitleId", "row_sequence": "seqNo",
        "detail_id": "bomTitleId", "detail_style": "style_code",
        "template_style": "正式款号",
    },
}


def load_profile(path=None):
    path = Path(path) if path is not None else Path(__file__).with_name("customer-profile.json")
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("客户配置包含未知项目。")
    profile = {**DEFAULTS, **value}
    profile["supplier_lookup"] = validate_supplier_settings(profile["supplier_lookup"])
    for key in ("fields", "query_defaults"):
        if not isinstance(profile[key], dict):
            raise ValueError(f"客户配置 {key} 必须是字段映射。")
        profile[key] = {**DEFAULTS[key], **profile[key]}
    if set(profile["fields"]) - set(DEFAULTS["fields"]) or any(
        not isinstance(item, str) or not item.strip() for item in profile["fields"].values()
    ):
        raise ValueError("客户业务字段映射无效。")
    if not isinstance(profile["compatibility_hosts"], list) or any(
        not isinstance(host, str) or not host for host in profile["compatibility_hosts"]
    ):
        raise ValueError("客户兼容域名配置无效。")
    for key in ("customer_id", "business_path", "query_endpoint", "detail_endpoint", "api_base_path"):
        if not isinstance(profile[key], str) or not profile[key].strip():
            raise ValueError(f"客户配置 {key} 无效。")
    for key in ("business_path", "query_endpoint", "detail_endpoint", "api_base_path"):
        parsed = urlparse(profile[key])
        if parsed.scheme or parsed.netloc:
            raise ValueError(f"客户配置 {key} 必须为同站点路径。")
    url = profile["business_url"]
    if not isinstance(url, str) or (url and (
        urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc
        or urlparse(url).username or urlparse(url).password
    )):
        raise ValueError("客户业务地址无效。")
    return profile


PROFILE = load_profile()


def business_url(settings):
    if PROFILE["business_url"]:
        return PROFILE["business_url"]
    login = settings.get("login_url", "")
    return urljoin(login, PROFILE["business_path"]) if login else PROFILE["business_path"]


def compatibility_host(hostname, settings):
    hosts = PROFILE["compatibility_hosts"] or [urlparse(settings.get("login_url", "")).hostname]
    return bool(hostname and hostname in hosts)
