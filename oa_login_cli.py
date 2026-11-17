#!/usr/bin/env python3
"""Local OA automation workbench. Run with Python, then use its browser UI."""

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from trim_template import _records, export_trim_xlsx
from license_manager import LicenseManager
from customer_profile import PROFILE, business_url, compatibility_host
from material_suppliers import (MaterialSupplierLookup, MAX_MAPPING_BYTES, load_supplier_mapping,
                                save_supplier_mapping, supplier_mapping_status, supplier_names)


APP_NAME = "oa-login-cli"
WORKBENCH_PORT = 54418
MAX_OUTPUT_LINES = 120
MAX_BODY_BYTES = 32_768
MAX_TEMPLATE_BYTES = 5 * 1024 * 1024
MAX_BATCH_CODES = 200
MAX_BATCH_FILE_BYTES = 5 * 1024 * 1024
MAX_BATCH_WORKERS = 3
LOGIN_COOKIE_RETENTION_SECONDS = 30 * 24 * 60 * 60
LOGIN_COOKIE_NAMES = frozenset({
    "is_login", "userSysTag", "language", "JSESSIONID", "SESSION", "SESSIONID",
    "sid", "token", "access_token", "auth_token", "rememberMe",
})
FIXED_BUSINESS_OPERATION = {
    "name": "工艺单打印",
    "module_id": "process_sheet",
    "kind": "process_sheet",
    "description": "输入款号并保存完整工艺单 PDF，可选择模板生成辅料 XLSX。",
}
CONFIG_LOCK = threading.Lock()
DIRECTORY_PICKER_LOCK = threading.Lock()


def configure_console() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def data_dir() -> Path:
    base = os.environ.get("APPDATA") if sys.platform == "win32" else None
    directory = Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def load_operations() -> List[dict]:
    path = data_dir() / "operations.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            return []
        return [
            item
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("url"), str)
            and item["name"].strip()
            and urlparse(item["url"]).scheme in {"http", "https"}
            and urlparse(item["url"]).netloc
        ]
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return []


def normalize_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("请输入有效的 http/https 地址。")
    return value


def load_settings() -> dict:
    path = data_dir() / "settings.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        return {"login_url": value.get("login_url", "")}
    operations = load_operations()
    login_url = next((item["login_url"] for item in operations if item.get("login_url")), "")
    if not login_url:
        login_url = next((item["url"] for item in operations if urlparse(item["url"]).path.lower().endswith("/login.html")), "")
    return {"login_url": login_url}


def load_print_settings() -> dict:
    path = data_dir() / "print-settings.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        saved = {}
    return {
        "original_dir": str(saved.get("original_dir", "")),
        "converted_dir": str(saved.get("converted_dir", "")),
        "original_history": list(saved.get("original_history", []))[:10],
        "converted_history": list(saved.get("converted_history", []))[:10],
        "templates": list(saved.get("templates", []))[:10],
        "template_id": str(saved.get("template_id", "")),
    }


def save_print_settings(settings: dict) -> None:
    target = data_dir() / "print-settings.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def output_directory(value: str) -> Path:
    path = Path(value.strip()).expanduser()
    if not value.strip() or not path.is_absolute():
        raise ValueError("请输入绝对保存目录，例如 C:\\工艺单 或 /Users/姓名/工艺单。")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or not os.access(path, os.W_OK):
        raise ValueError(f"保存目录不可写：{path}")
    return path


def choose_directory(initial: str) -> str:
    if not DIRECTORY_PICKER_LOCK.acquire(blocking=False):
        raise ValueError("文件夹选择窗口已打开，请先完成选择。")
    try:
        start = Path(initial).expanduser() if initial else Path.home()
        if not start.is_dir():
            start = Path.home()
        if sys.platform == "darwin":
            script = '''on run argv
                try
                    activate
                    return POSIX path of (choose folder with prompt "选择保存目录" default location (POSIX file (item 1 of argv)))
                on error number -128
                    return ""
                end try
            end run'''
            command = ["osascript", "-e", script, str(start)]
            environment = None
        elif sys.platform == "win32":
            script = '''[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
                Add-Type -AssemblyName System.Windows.Forms
                $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
                $dialog.Description = 'Select output folder'
                $dialog.SelectedPath = $env:AIOA_PICKER_INITIAL
                $dialog.ShowNewFolderButton = $true
                try {
                    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
                        [Console]::Write($dialog.SelectedPath)
                    }
                } finally { $dialog.Dispose() }'''
            command = ["powershell.exe", "-NoProfile", "-STA", "-Command", script]
            environment = dict(os.environ, AIOA_PICKER_INITIAL=str(start))
        else:
            raise ValueError("文件夹选择目前支持 Windows 和 macOS。")
        result = subprocess.run(command, capture_output=True, encoding="utf-8", env=environment)
        if result.returncode:
            raise ValueError("无法打开系统文件夹选择窗口，请重试。")
        return result.stdout.strip()
    finally:
        DIRECTORY_PICKER_LOCK.release()


def remember_directory(settings: dict, field: str, value: str) -> None:
    path = str(output_directory(value))
    settings[field] = path
    history = field.replace("_dir", "_history")
    settings[history] = [path] + [item for item in settings[history] if item != path][:9]


def store_template(filename: str, encoded: str) -> dict:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("模板必须是 .xlsx 文件。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("模板文件编码无效。")
    if not content or len(content) > MAX_TEMPLATE_BYTES:
        raise ValueError("模板大小须在 5 MB 以内。")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if "xl/workbook.xml" not in names or sum(item.file_size for item in archive.infolist()) > 25 * 1024 * 1024:
                raise ValueError("模板不是有效的 XLSX 工作簿，或解压后过大。")
    except zipfile.BadZipFile:
        raise ValueError("模板不是有效的 XLSX 工作簿。")
    template_id = hashlib.sha256(content).hexdigest()[:20]
    folder = data_dir() / "templates"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{template_id}.xlsx"
    if not target.exists():
        target.write_bytes(content)
    return {"id": template_id, "name": Path(filename).name, "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M")}


def read_style_codes(filename: str, encoded: str) -> List[str]:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("批量款号文件必须是 .xlsx 格式。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("批量款号文件编码无效。")
    if not content or len(content) > MAX_BATCH_FILE_BYTES:
        raise ValueError("批量款号文件大小须在 5 MB 以内。")
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as error:
        raise ValueError(f"无法读取批量款号 XLSX 文件：{error}") from error
    try:
        sheet = workbook.worksheets[0]
        codes = []
        for row in sheet.iter_rows(min_col=1, max_col=1, values_only=True):
            value = str(row[0] or "").strip().upper()
            if not value:
                continue
            if not re.fullmatch(r"[A-Z0-9_-]{1,80}", value):
                raise ValueError(f"第一列包含无效款号：{value[:80]}")
            codes.append(value)
            if len(codes) > MAX_BATCH_CODES:
                raise ValueError(f"每批最多处理 {MAX_BATCH_CODES} 个款号。")
        if not codes:
            raise ValueError("XLSX 第一列没有可处理的款号。")
        if len(set(codes)) != len(codes):
            raise ValueError("XLSX 第一列有重复款号，请先去重后再运行。")
        return codes
    finally:
        workbook.close()


def business_operations(settings: dict) -> List[dict]:
    return [dict(FIXED_BUSINESS_OPERATION, url=business_url(settings),
                 supplier_lookup_enabled=PROFILE["supplier_lookup"]["enabled"])]


def visible_locator(
    frame: Any, selectors: List[str], require_editable: bool = False
):
    for selector in selectors:
        locator = frame.locator(selector)
        for index in range(min(locator.count(), 10)):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                if require_editable and not candidate.is_editable():
                    continue
                return candidate
            except Exception:
                continue
    return None


def find_login_form(page: Any):
    username_selectors = [
        'input#username',
        'input[autocomplete="username"]',
        'input[type="email"]',
        'input[placeholder*="用户名"]',
        'input[placeholder*="账号"]',
        'input[placeholder*="工号"]',
        'input[name*="user" i]',
        'input[id*="user" i]',
        'input[name*="account" i]',
        'input[id*="account" i]',
        'input[name*="login" i]',
        'input[id*="login" i]',
        'input[type="text"]',
    ]
    password_selectors = [
        'input#password',
        'input[autocomplete="current-password"]',
        'input[type="password"]',
    ]

    for frame in page.frames:
        password = visible_locator(frame, password_selectors, require_editable=True)
        username = visible_locator(frame, username_selectors, require_editable=True)
        if username is not None and password is not None:
            return frame, username, password
    return None, None, None


def fill_login_form(page: Any, username: str, password: str):
    deadline = time.monotonic() + 20
    last_error = None
    while time.monotonic() < deadline:
        frame, username_input, password_input = find_login_form(page)
        if frame is not None:
            try:
                username_input.fill(username, timeout=3_000)
                password_input.fill(password, timeout=3_000)
                return frame, password_input
            except Exception as error:
                last_error = error
        page.wait_for_timeout(500)

    detail = f"：{last_error}" if last_error else ""
    raise RuntimeError(f"无法填写可编辑的用户名或密码输入框{detail}")


def click_login(frame: Any, password_locator: Any) -> None:
    submit_selectors = [
        'button.btn_submit[data-i18n="common_login"]',
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("登录")',
        'button:has-text("登陆")',
        'button:has-text("Login")',
        '[role="button"]:has-text("登录")',
    ]
    button = visible_locator(frame, submit_selectors)
    if button is not None:
        button.click()
    else:
        password_locator.press("Enter")


def same_page(left: str, right: str) -> bool:
    first, second = urlparse(left), urlparse(right)
    return (first.scheme, first.netloc, first.path) == (
        second.scheme, second.netloc, second.path
    )


def is_process_sheet(operation: dict) -> bool:
    if operation.get("kind") == "process_sheet":
        return True
    url = urlparse(operation.get("url", ""))
    return url.path.lower().endswith("/plm/stylemanage/newstylemanage.html")


def open_business_page(page: Any, operation: dict, wait_until: str = "domcontentloaded") -> None:
    target = operation["url"]
    if (urlparse(target).path.lower().endswith("/login.html")
            or (operation.get("login_url") and same_page(target, operation["login_url"]))):
        raise ValueError("业务地址指向登录页，已阻止主动跳转。")
    page.goto(target, wait_until=wait_until, timeout=60_000)


def captcha_visible(page: Any) -> bool:
    for frame in page.frames:
        try:
            image = frame.locator("img.kaptcha_img").first
            if image.is_visible():
                return True
        except Exception:
            continue
    return False


def login_error_text(page: Any) -> str:
    for frame in page.frames:
        try:
            label = frame.locator("label.my_message").first
            if label.is_visible():
                return label.inner_text(timeout=1_000).strip()
        except Exception:
            continue
    return ""


def main_page_text(page: Any) -> str:
    useful = []
    seen = set()
    for frame in page.frames:
        try:
            raw = frame.locator("body").inner_text(timeout=5_000)
        except Exception:
            continue
        for line in raw.splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if clean and clean not in seen:
                useful.append(clean)
                seen.add(clean)
    if len(useful) > MAX_OUTPUT_LINES:
        useful = useful[:MAX_OUTPUT_LINES] + ["……（内容已截断）"]
    return "\n".join(useful)


def save_process_sheet_pdf(context: Any, page: Any, operation: dict, row: dict) -> tuple:
    style_code = operation["style_code"]
    directory = output_directory(operation["print_settings"]["original_dir"])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{style_code}_次数{row['seq']}_{timestamp}"
    destination = directory / f"{stem}.pdf"
    suffix = 2
    while destination.exists():
        destination = directory / f"{stem}_{suffix}.pdf"
        suffix += 1

    page.locator("#fcl").wait_for(state="visible", timeout=60_000)
    # Vue mounts the visible section before getAllData populates its material groups.
    # Even an empty result is padded into a group by OA, so this also permits a
    # genuinely empty sheet to finish loading and report its normal conversion error.
    page.wait_for_function("""() => {
      const section = document.querySelector('#fcl');
      const vm = document.querySelector('#app')?.__vue__;
      if (document.querySelector('#app') && (!vm || !vm.bomItemX?.length)) return false;
      const cells = section ? [...section.querySelectorAll('td.eachLie')] : [];
      return cells.length > 0 && cells.every(cell => !cell.textContent.includes('{{'));
    }""", timeout=60_000)
    trim_tables = []
    if operation["print_settings"].get("template_id"):
        trim_tables = page.locator("#fcl").evaluate("""element => {
      const sections = [element];
      // OA renders the second and remainder material pages as sibling sections.
      for (let next = element.nextElementSibling;
           next && next.querySelector('td.eachLie'); next = next.nextElementSibling) {
        sections.push(next);
      }
      // Each tbody represents a separate group of materials; combining groups
      // would overwrite repeated field names when transposing the table.
      return sections.flatMap(section => [...section.querySelectorAll('tbody')])
        .filter(body => body.querySelector('td.eachLie'))
        .map(body => [...body.rows].filter(row => row.querySelector('td.eachLie'))
          .map(row => [...row.cells].map(cell => cell.innerText.trim())));
    }""")
    page.evaluate("""() => {
      window.__aioaPrintHtml = null;
      const original = jQuery.fn.print;
      jQuery.fn.print = function(options) {
        jQuery.fn.print = original;
        const copy = this.clone();
        if (options.noPrintSelector) copy.find(options.noPrintSelector).remove();
        for (const marker of copy[0].querySelectorAll('table span[data-i18n="reqStorageDate"]')) {
          marker.closest('th,td')?.remove();
        }
        const styles = [...document.querySelectorAll('link[rel="stylesheet"],style')]
          .map(node => node.outerHTML).join('');
        const base = document.createElement('base'); base.href = location.href;
        window.__aioaPrintHtml = '<!doctype html><html><head>'+base.outerHTML+styles+
          '</head><body>'+copy[0].outerHTML+'</body></html>';
        return jQuery.Deferred().resolve();
      };
    }""")
    page.locator("button.printBtn").click()
    page.wait_for_function("() => !!window.__aioaPrintHtml", timeout=15_000)
    html = page.evaluate("() => window.__aioaPrintHtml")
    print_page = context.new_page()
    temporary = destination.with_suffix(".tmp.pdf")
    try:
        print_page.set_content(html, wait_until="domcontentloaded")
        print_page.wait_for_function("() => [...document.images].every(image => image.complete)", timeout=45_000)
        print_page.pdf(path=str(temporary), format="A4", landscape=True,
                       prefer_css_page_size=True, print_background=True)
        os.replace(temporary, destination)
    finally:
        print_page.close()
        if temporary.exists():
            temporary.unlink()

    if not operation["print_settings"].get("template_id"):
        return destination, None, trim_tables
    source_dir = data_dir() / "trim-sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / f"{hashlib.sha256(str(destination).encode()).hexdigest()[:20]}.json"
    source.write_text(json.dumps({"style_code": style_code, "row": row,
                                  "tables": trim_tables}, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination, source, trim_tables


def launch_context(playwright: Any) -> Any:
    profile = data_dir() / "browser-profile"
    try:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=profile, channel="msedge", headless=False
        )
    except Exception as edge_error:
        print(f"Edge 启动失败，尝试 Chromium：{edge_error}")
        return playwright.chromium.launch_persistent_context(
            user_data_dir=profile, headless=False
        )


def preserve_login_session(context: Any, operation: dict) -> bool:
    """Keep authenticated session cookies in Chromium's existing user profile."""
    business_url = operation["url"]
    urls = [business_url]
    login_url = operation.get("login_url") or ""
    if login_url and urlparse(login_url).hostname == urlparse(business_url).hostname:
        urls.append(login_url)
    cookies = context.cookies(urls)
    if not any(cookie["name"] == "is_login" and cookie["value"] == "true" for cookie in cookies):
        return False
    expires = time.time() + LOGIN_COOKIE_RETENTION_SECONDS
    session_cookies = []
    for cookie in cookies:
        if (cookie["name"] in LOGIN_COOKIE_NAMES or cookie["name"].startswith("u_")) and cookie.get("expires", -1) <= 0:
            persistent = dict(cookie)
            persistent["expires"] = expires
            session_cookies.append(persistent)
    if session_cookies:
        context.add_cookies(session_cookies)
    return True


LOGIN_SUBMIT_GUARD = r"""
(() => {
  function guard(event) {
    if (!location.pathname.endsWith('/activiti/html/login.html')) return;
    const form = document.querySelector('form#loginForm');
    const button = form && form.querySelector('.btn_submit');
    if (!form || !button) return;
    const submit = event.type === 'submit' && event.target === form;
    const click = event.type === 'click' && button.contains(event.target);
    if (!submit && !click) return;
    const ready = window.jQuery && window.SYS && SYS.login &&
      jQuery._data(button, 'events')?.click?.length;
    // NOAH authenticates using its click handler, never a native form submit.
    if (submit || !ready) event.preventDefault();
    if (!ready) {
      event.stopImmediatePropagation();
      const message = form.querySelector('.my_message');
      if (message) message.textContent = '登录脚本尚未就绪，请稍候再点击登录；若持续出现，请检查网络或联系管理员。';
    }
  }
  document.addEventListener('click', guard, true);
  document.addEventListener('submit', guard, true);
})();
"""


def install_noah_login_compatibility(context: Any) -> None:
    # NOAH's legacy shims omit dependencies used during module initialization.
    # Patch only the login page's loader response, leaving server files intact.
    def patch_loader(route: Any) -> None:
        request = route.request
        page_url = urlparse(request.frame.url)
        if (not compatibility_host(page_url.hostname, load_settings())
                or urlparse(request.url).hostname != page_url.hostname
                or page_url.path not in {"/activiti/html/login.html", "/activiti/html/plm/styleManage/newStyleManage.html", "/activiti/html/plm/styleManage/printAll.html"}):
            route.continue_()
            return
        response = route.fetch()
        source = response.text()
        marker = "require(['oo', 'doT',"
        dependencies = """
require.config({shim: {
  'common': {deps: ['oo', 'jquery.lazyload', 'bootstrap', 'jquery.cookie', 'jquery.properties']},
  'translate': {deps: ['jquery', 'jquery.properties', 'jquery.cookie']},
  'translator': {deps: ['jquery', 'oo']},
  'noah': {deps: ['oo', 'jquery', 'base64', 'dialog', 'common']},
  'sys-tool': {deps: ['oo', 'base64', 'zi_pagination', 'jqueryUi', 'common']}
}});
"""
        if marker in source:
            source = source.replace(marker, dependencies + marker, 1)
        route.fulfill(response=response, body=source)

    context.route("**/activiti/js/main.js*", patch_loader)


def install_noah_print_compatibility(context: Any) -> None:
    def patch_print_script(route: Any) -> None:
        request_url = urlparse(route.request.url)
        page_url = urlparse(route.request.frame.url)
        if (not compatibility_host(page_url.hostname, load_settings())
                or request_url.hostname != page_url.hostname
                or request_url.path != "/activiti/js/plm/styleManage/printAll.js"
                or page_url.path != "/activiti/html/plm/styleManage/printAll.html"):
            route.continue_()
            return
        response = route.fetch()
        source = re.sub(r"(?m)^[ \t]*debugger[ \t]*;?[ \t]*\r?$", "", response.text())
        route.fulfill(response=response, body=source)

    context.route("**/activiti/js/plm/styleManage/printAll.js*", patch_print_script)


class Cancelled(Exception):
    pass


class AutomationController:
    """Owns the Playwright worker and exposes only non-secret state to HTTP."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.messages = queue.Queue()
        self.jobs = queue.Queue()
        self.cancel_event = threading.Event()
        self.batch_local = threading.local()
        self.shutdown_event = threading.Event()
        self.worker = None
        self.task_active = False
        self.state = {
            "phase": "idle",
            "step": "open",
            "login_encountered": False,
            "message": "选择业务操作，开始连接 OA 页面。",
            "operation": "",
            "title": "",
            "url": "",
            "content": "",
            "updated_at": "",
        }

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def update(self, phase: str, message: str, **values: Any) -> None:
        with self.lock:
            if phase in {"login_required", "authenticating"}:
                values["login_encountered"] = True
                values["step"] = "login"
            self.state.update(
                phase=phase,
                message=message,
                updated_at=datetime.now().strftime("%H:%M:%S"),
                **values,
            )

    def start(self, operation: dict) -> None:
        module_id = operation.get("module_id")
        if not isinstance(module_id, str) or not module_id.strip():
            raise ValueError("该业务尚未配置授权模块，请联系管理员。")
        LICENSES.require_valid(feature=module_id)
        with self.lock:
            if self.shutdown_event.is_set():
                raise ValueError("工作台正在退出，无法启动新任务。")
            if self.task_active:
                raise ValueError("已有任务正在运行，请先等待或停止。")
            with CONFIG_LOCK:
                name_mapping = (load_supplier_mapping(data_dir())["entries"]
                                if operation.get("print_settings", {}).get("template_id")
                                and PROFILE["supplier_lookup"]["enabled"] else {})
            operation = dict(operation, supplier_name_mapping=name_mapping)
            self.messages = queue.Queue()
            self.cancel_event.clear()
            self.task_active = True
            self.state = {
                "phase": "opening",
                "step": "open",
                "login_encountered": False,
                "message": "正在打开业务页面…",
                "operation": operation["name"],
                "batch_total": len(operation.get("style_codes", [])) or 1,
                "batch_completed": 0,
                "title": "",
                "url": "",
                "content": "",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._worker_loop, daemon=True)
                self.worker.start()
            self.jobs.put(dict(operation))

    def submit_credentials(self, username: str, password: str) -> None:
        with self.lock:
            if self.state["phase"] != "login_required":
                raise ValueError("当前无需填写登录信息。")
        if not username.strip() or not password:
            raise ValueError("用户名和密码不能为空。")
        self.messages.put(("credentials", username.strip(), password))
        self.update("authenticating", "正在填写登录信息…")

    def stop(self) -> None:
        with self.lock:
            if not self.task_active:
                raise ValueError("当前没有运行中的任务。")
            self.cancel_event.set()
        self.messages.put(("cancel",))
        self.update("stopping", "正在停止任务…")

    def shutdown(self) -> None:
        with self.lock:
            self.shutdown_event.set()
            self.cancel_event.set()
        self.messages.put(("cancel",))
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join()

    def check_cancelled(self) -> None:
        batch_abort = getattr(self.batch_local, "abort", None)
        if self.cancel_event.is_set() or (batch_abort is not None and batch_abort.is_set()):
            raise Cancelled()

    def _login_form_visible(self, page: Any) -> bool:
        try:
            return find_login_form(page)[0] is not None
        except Exception:
            return False

    def _on_login_page(self, page: Any, operation: dict) -> bool:
        if self._login_form_visible(page):
            return True
        login_url = operation.get("login_url")
        if login_url and same_page(page.url, login_url):
            return True
        return urlparse(page.url).path.lower().endswith("/login.html")

    def _wait_for_login(self, page: Any, operation: dict) -> None:
        if operation.get("batch_isolated"):
            operation["_batch_auth_failure"].set()
            operation["_batch_abort"].set()
            raise RuntimeError("并发任务的登录会话已失效；请结束当前批次并重新运行。")
        submitted_at = None
        first_seen_at = time.monotonic()
        form_seen = False
        last_login_error = ""
        self.update(
            "login_required",
            "检测到登录页面。可在此填写账号密码；验证码请在已打开的 OA 浏览器中手工完成。",
        )
        while True:
            self.check_cancelled()
            if self._login_form_visible(page):
                form_seen = True
            try:
                message = self.messages.get(timeout=1)
            except queue.Empty:
                message = None

            if message and message[0] == "credentials":
                _, username, password = message
                last_login_error = ""
                try:
                    frame, password_input = fill_login_form(page, username, password)
                    if captcha_visible(page):
                        self.update(
                            "login_required",
                            "账号密码已填入 OA 页面。请在 OA 浏览器中输入验证码并点击“登录”。",
                        )
                    else:
                        click_login(frame, password_input)
                        self.update("authenticating", "已提交登录信息，正在等待页面跳转…")
                        submitted_at = time.monotonic()
                except Exception as error:
                    self.update("login_required", f"自动填写失败：{error}。可在 OA 浏览器中手动登录。")
                finally:
                    password = ""

            self.check_cancelled()
            error_text = login_error_text(page)
            if error_text and error_text != last_login_error:
                self.update("login_required", f"登录页提示：{error_text}")
                last_login_error = error_text

            if form_seen and not self._on_login_page(page, operation):
                page.wait_for_timeout(1100)
                if not self._on_login_page(page, operation):
                    try:
                        preserve_login_session(page.context, operation)
                    except Exception:
                        self.update("opening", "登录成功，但登录状态未能保存；关闭浏览器后可能需要重新登录。")
                    return
            elif submitted_at and time.monotonic() - submitted_at > 10:
                self.update(
                    "login_required",
                    "登录页仍未跳转。请检查页面提示，或在 OA 浏览器中继续操作。",
                )
                submitted_at = None
            elif not form_seen and time.monotonic() - first_seen_at > 30:
                raise RuntimeError("登录页加载超时：未找到可填写的用户名和密码输入框。")

    def _wait_for_process_sheet(self, page: Any, operation: dict) -> None:
        deadline = time.monotonic() + 60
        recoveries = 0
        while time.monotonic() < deadline:
            self.check_cancelled()
            if self._on_login_page(page, operation):
                if recoveries >= 2:
                    raise RuntimeError("业务页反复跳转登录，请检查该账号的业务权限或会话状态。")
                self._wait_for_login(page, operation)
                self.check_cancelled()
                self.update("opening", "登录完成，正在继续工艺单打印…", step="open")
                if not same_page(page.url, operation["url"]):
                    open_business_page(page, operation)
                recoveries += 1
                deadline = time.monotonic() + 60
            elif not same_page(page.url, operation["url"]):
                if recoveries >= 2:
                    raise RuntimeError(f"无法进入工艺单列表，当前页面：{page.url}")
                self.update("opening", "正在从当前页面返回工艺单列表…", step="open")
                open_business_page(page, operation)
                recoveries += 1
            else:
                try:
                    ready = page.evaluate("""() => !!(window.jQuery &&
                      document.querySelector('input[name="styleCode"].searchMatnr') &&
                      jQuery('#tb_user').data('bootstrap.table'))""")
                    if ready:
                        return
                except Exception:
                    # Navigation may destroy the JS context between checks.
                    if page.is_closed():
                        raise RuntimeError("OA 浏览器页面已关闭。")
            page.wait_for_timeout(250)
        raise RuntimeError(f"工艺单列表初始化超时，当前页面：{page.url}。请检查 OA 页面提示或脚本加载状态。")

    def _post_oa_json(self, context: Any, page: Any, operation: dict,
                      path: str, data: dict, step: str = "query") -> dict:
        base = urljoin(operation["url"], PROFILE["api_base_path"])
        for attempt in range(2):
            self.check_cancelled()
            response = context.request.post(
                urljoin(base, path), data=data,
                headers={"Content-Type": "application/json"}, timeout=60_000,
            )
            expired = response.status in (401, 403) or urlparse(response.url).path.lower().endswith("/login.html")
            if not expired:
                break
            if operation.get("batch_isolated"):
                operation["_batch_auth_failure"].set()
                operation["_batch_abort"].set()
                raise RuntimeError("OA 登录已失效，已停止并发任务。")
            if attempt or not operation.get("login_url"):
                raise RuntimeError("OA 接口拒绝访问，请检查登录状态或账号权限。")
            for cookie in context.cookies(operation["url"]):
                if cookie["name"] == "is_login":
                    context.clear_cookies(name="is_login", domain=cookie["domain"], path=cookie["path"])
            page.goto(operation["login_url"], wait_until="commit", timeout=60_000)
            self._wait_for_login(page, operation)
            self.update("reading", "登录完成，正在继续接口请求…", step=step)
        self.check_cancelled()
        if not response.ok:
            raise RuntimeError(f"OA 接口请求失败：HTTP {response.status}。")
        try:
            payload = response.json()
        except Exception as error:
            if operation.get("batch_isolated"):
                operation["_batch_auth_failure"].set()
                operation["_batch_abort"].set()
            raise RuntimeError("OA 接口没有返回 JSON，可能需要重新登录。") from error
        if not isinstance(payload, dict) or payload.get("code") != 10000:
            message = payload.get("message", "响应格式异常") if isinstance(payload, dict) else "响应格式异常"
            raise RuntimeError(f"OA 接口返回错误：{message}")
        return payload

    def _supplier_export_options(self, context: Any, page: Any, operation: dict) -> dict:
        settings = PROFILE["supplier_lookup"]
        if not settings["enabled"]:
            return {}
        lookup = MaterialSupplierLookup(
            lambda path, data: self._post_oa_json(context, page, operation, path, data, step="suppliers"),
            settings,
        )

        def find_suppliers(code: str) -> tuple:
            self.check_cancelled()
            self.update("reading", f"正在查询辅料 {code} 的供应商…", step="suppliers")
            return supplier_names(lookup(code), operation.get("supplier_name_mapping", {}))

        return {"supplier_lookup": find_suppliers, "material_header": settings["material_header"]}

    def _print_process_sheets(self, context: Any, page: Any, operation: dict) -> dict:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        style_code = operation["style_code"]
        fields = PROFILE["fields"]
        self.check_cancelled()
        self.update("reading", f"正在通过接口查询款号 {style_code}…", step="query")
        language = next((item["value"] for item in context.cookies(operation["url"])
                         if item["name"] == "language"), "zh-cn") or "zh-cn"
        flags = {name: True for name in (
            "mainPic", "mainPart", "qhQty", "detailPic", "fcl",
            "sizeDetail", "trouble", "BOMConfirm",
        )}
        flags["modifyRecord"] = False
        config = {
            "base": urljoin(operation["url"], PROFILE["api_base_path"]),
            "defaults": dict(PROFILE["query_defaults"], langu=language),
            "language": language,
            "printLanguage": language,
        }
        context.add_cookies([
            {"name": "printFlagObj", "value": quote(json.dumps(flags, separators=(",", ":"))), "url": operation["url"]},
            {"name": "printLanguage", "value": quote(language), "url": operation["url"]},
        ])

        def post_oa(path: str, data: dict) -> dict:
            return self._post_oa_json(context, page, operation, path, data)

        rows = []
        page_number = 1
        page_size = 999
        while True:
            query = dict(config.get("defaults") or {},
                         pageNum=page_number, pageSize=page_size, language=config["language"])
            query[fields["query_style"]] = style_code
            payload = post_oa(PROFILE["query_endpoint"], query)
            result = payload.get("data")
            if not isinstance(result, dict) or not isinstance(result.get("list"), list):
                raise RuntimeError("工艺单查询接口返回格式异常。")
            batch = result["list"]
            rows.extend(batch)
            total_pages = result.get("pageCount")
            try:
                total_pages = int(total_pages) if total_pages is not None else None
            except (TypeError, ValueError):
                raise RuntimeError("工艺单查询接口返回了无效的页数。")
            if not batch or (total_pages is not None and page_number >= total_pages):
                break
            if total_pages is None and len(batch) < page_size:
                break
            page_number += 1
            if page_number > 200:
                raise RuntimeError("工艺单查询页数超过上限，已停止。")

        matching = [item for item in rows if isinstance(item, dict) and
                    re.sub(r"\s+", "", str(item.get(fields["row_style"], ""))).upper() == style_code]
        if not matching:
            samples = ", ".join(str(row.get(fields["row_style"], "")) for row in rows[:5] if isinstance(row, dict))
            raise RuntimeError(f"没有与款号 {style_code} 完全匹配的工艺单；返回款号示例：{samples}")
        seen = set()
        printed = []
        print_url = urljoin(operation["url"], "printAll.html")
        for index, item in enumerate(matching):
            self.check_cancelled()
            key = str(item.get(fields["row_id"]) or "")
            if not key or key in seen:
                raise RuntimeError("查询结果缺少唯一编号或存在重复记录，已停止以避免重复打印。")
            seen.add(key)
            row = {"id": key, "style": item.get(fields["row_style"], ""), "seq": item.get(fields["row_sequence"], "")}
            self.update("reading", f"正在获取第 {index+1} 份工艺单（{style_code}，次数 {row['seq']}）…", step="select")
            detail = post_oa(PROFILE["detail_endpoint"], {fields["detail_id"]: key}).get("data")
            if not isinstance(detail, dict) or not isinstance(detail.get("bomTitle"), dict):
                raise RuntimeError("工艺单明细接口返回格式异常。")
            params = {"bomtitleid": key, "stylecode": row["style"], "printLangu": config["printLanguage"]}
            for field in ("zzitemCd", "lgrop", "mgrop", "saiso", "saisj"):
                params[field] = item.get(field) or ""
            params["zzdsnno"] = item.get("zzdsnNo") or item.get("zzdsnno") or ""
            if index:
                page = context.new_page()
            page.goto(print_url + "?" + urlencode(params), wait_until="commit", timeout=60_000)
            if self._on_login_page(page, operation):
                self._wait_for_login(page, operation)
                raise RuntimeError("打开工艺单页面时登录已失效，请重新运行。")
            actual_id = parse_qs(urlparse(page.url).query).get("bomtitleid", [None])[0]
            if actual_id != key:
                raise RuntimeError(f"打开的工艺单与接口返回不一致（预期 {key}，实际 {actual_id}）。")
            for attempt in range(3):
                try:
                    page.wait_for_function("() => !!document.querySelector('#app')?.__vue__", timeout=30_000)
                    break
                except PlaywrightTimeoutError as error:
                    self.check_cancelled()
                    if self._on_login_page(page, operation):
                        self._wait_for_login(page, operation)
                        raise RuntimeError("打印页面登录已失效，请重新运行。") from error
                    if attempt == 2:
                        raise RuntimeError("OA 打印页面脚本加载失败，重试后仍无法初始化。") from error
                    self.update("opening", "OA 打印页面加载不完整，正在重试…", step="select")
                    page.reload(wait_until="commit", timeout=60_000)
            self.check_cancelled()
            self.update("reading", f"正在保存第 {index+1} 份完整工艺单 PDF…", step="save_pdf")
            saved_pdf, trim_source, trim_tables = save_process_sheet_pdf(context, page, operation, row)
            template_id = operation["print_settings"].get("template_id", "")
            if not template_id:
                printed.append(f"{style_code} / 次数 {row['seq']}：完整PDF {saved_pdf}；未选择模板，未生成辅料 XLSX")
                continue
            official_style = str(detail["bomTitle"].get(fields["detail_style"]) or row["style"]).strip()
            if operation.get("_batch_trim") is not None:
                records = _records(trim_tables, {fields["template_style"]: official_style})
                if not records:
                    raise RuntimeError(f"完整工艺单已保存：{saved_pdf}；OA 打印页的辅料信息表没有可转换的数据。")
                with operation["_batch_trim_lock"]:
                    operation["_batch_trim"].append((style_code, index, records))
                printed.append(f"{style_code} / 次数 {row['seq']}：完整PDF {saved_pdf}；辅料信息待批量汇总（{len(records)} 条）")
                continue
            self.update("reading", f"正在转换第 {index+1} 份辅料信息 XLSX…", step="convert")
            template_path = data_dir() / "templates" / f"{template_id}.xlsx"
            converted_dir = output_directory(operation["print_settings"]["converted_dir"])
            converted_xlsx = converted_dir / f"{saved_pdf.stem}_辅料信息.xlsx"
            version = 2
            while converted_xlsx.exists():
                converted_xlsx = converted_dir / f"{saved_pdf.stem}_辅料信息_{version}.xlsx"
                version += 1
            try:
                conversion = export_trim_xlsx(template_path, trim_tables, converted_xlsx,
                    shared_fields={fields["template_style"]: official_style},
                    **self._supplier_export_options(context, page, operation))
            except Cancelled:
                raise
            except Exception as error:
                raise RuntimeError(f"完整工艺单已保存：{saved_pdf}；辅料模板转换失败：{error}") from error
            printed.append(f"{style_code} / 次数 {row['seq']}：完整PDF {saved_pdf}；辅料XLSX {converted_xlsx}（{conversion['rows']} 条）")
            if conversion["supplier_missing"]:
                printed.append(f"提示：{conversion['supplier_missing']} 行辅料没有供应商信息，对应供应商单元格留空。")
        return {"title": f"{style_code} · 工艺单打印", "url": operation["url"], "content": "\n".join(printed)}

    def _worker_loop(self) -> None:
        context = None
        context_closed = threading.Event()
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                while not self.shutdown_event.is_set():
                    try:
                        operation = self.jobs.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        if context is not None and not context_closed.is_set():
                            try:
                                context.cookies(operation["url"])
                            except Exception:
                                context_closed.set()
                        if context is None or context_closed.is_set():
                            context = launch_context(playwright)
                            context_closed = threading.Event()
                            context.on("close", lambda *_, closed=context_closed: closed.set())
                            context.add_init_script(LOGIN_SUBMIT_GUARD)
                            install_noah_login_compatibility(context)
                            install_noah_print_compatibility(context)
                        if len(operation.get("style_codes", [])) > 1:
                            self._run_batch(operation, context, playwright)
                        else:
                            if operation.get("style_codes"):
                                operation["style_code"] = operation["style_codes"][0]
                            self._run(operation, context)
                    except Exception as error:
                        self.update("error", f"执行失败：{error}")
                        if context is not None:
                            try:
                                context.close()
                            except Exception:
                                pass
                            context = None
                    finally:
                        with self.lock:
                            self.task_active = False
        except Exception as error:
            self.update("error", f"浏览器服务启动失败：{error}")
            with self.lock:
                self.task_active = False
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    def _run_batch(self, operation: dict, primary_context: Any, playwright: Any) -> None:
        from playwright.sync_api import sync_playwright

        codes = operation["style_codes"]
        page = primary_context.pages[0] if primary_context.pages else primary_context.new_page()
        self.update("opening", "正在检查 OA 会话，为并发任务准备登录状态…", step="open")
        open_business_page(page, operation)
        page.wait_for_timeout(1_800)
        self.check_cancelled()
        if self._on_login_page(page, operation):
            self._wait_for_login(page, operation)
            if not same_page(page.url, operation["url"]):
                open_business_page(page, operation)
                page.wait_for_timeout(1_200)
            if self._on_login_page(page, operation):
                raise RuntimeError("登录未完成，批量任务尚未启动。")
        if not same_page(page.url, operation["url"]):
            raise RuntimeError(f"未到达工艺单列表，当前地址：{page.url}")
        target = urlparse(operation["url"])
        target_origin = f"{target.scheme}://{target.netloc}"
        session_storage = page.evaluate("""() => Object.fromEntries(
            Array.from({length: sessionStorage.length}, (_, index) => {
                const key = sessionStorage.key(index);
                return [key, sessionStorage.getItem(key)];
            })
        )""")

        def make_session_script(values: dict) -> str:
            return (
                "(() => { if (location.origin !== " + json.dumps(target_origin) + ") return; "
                "for (const [key, value] of Object.entries(" + json.dumps(values) + ")) "
                "sessionStorage.setItem(key, value); })();"
            )

        results = {}
        operation = dict(operation, _batch_trim=[], _batch_trim_lock=threading.Lock())

        def finish(phase: str, message: str) -> None:
            completed = len(results)
            lines = [f"{code}：{results[code].get('content') or results[code].get('message', results[code]['phase'])}"
                     if code in results else f"{code}：未执行" for code in codes]
            if operation["_batch_trim"]:
                try:
                    self.update("reading", "正在汇总已完成款号的辅料信息…", step="convert")
                    order = {code: index for index, code in enumerate(codes)}
                    groups = sorted(operation["_batch_trim"], key=lambda item: (order[item[0]], item[1]))
                    records = [record for _, _, group in groups for record in group]
                    fields = list(dict.fromkeys(field for record in records for field in record))
                    table = [[field] + [record.get(field, "") for record in records] for field in fields]
                    directory = output_directory(operation["print_settings"]["converted_dir"])
                    stem = f"批量辅料信息_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
                    destination = directory / f"{stem}.xlsx"
                    version = 2
                    while destination.exists():
                        destination = directory / f"{stem}_{version}.xlsx"
                        version += 1
                    template = data_dir() / "templates" / f"{operation['print_settings']['template_id']}.xlsx"
                    supplier_options = self._supplier_export_options(primary_context, page, operation) if phase == "done" else {}
                    conversion = export_trim_xlsx(template, [table], destination, **supplier_options)
                    lines.append(f"辅料汇总 XLSX：{destination}（{conversion['rows']} 条）")
                    if conversion["supplier_missing"]:
                        lines.append(f"提示：{conversion['supplier_missing']} 行辅料没有供应商信息，对应供应商单元格留空。")
                    if phase != "done" and PROFILE["supplier_lookup"]["enabled"]:
                        lines.append("本批任务未完整完成，已保留基础辅料汇总，供应商信息尚未补充。")
                    message += " 已生成辅料汇总文件。"
                except Cancelled:
                    phase = "cancelled"
                    message = "任务已停止，供应商查询未完成；已保存的工艺单 PDF 保留。"
                except Exception as error:
                    phase = "error" if phase != "cancelled" else phase
                    message += f" 辅料汇总失败：{error}"
            self.update(phase, message, step="convert" if operation["print_settings"].get("template_id") else "save_pdf", title=f"工艺单批量打印（{len(codes)} 个款号）",
                        url=operation["url"], content="\n".join(lines),
                        batch_total=len(codes), batch_completed=completed)

        # Run one complete job in the already authenticated primary context.
        first = codes[0]
        self.update("reading", f"先处理第一个款号 {first}，完成后再启动并发任务…",
                    step="query", batch_total=len(codes), batch_completed=0)
        first_result = self._run(dict(operation, style_code=first), primary_context, publish=False)
        results[first] = first_result
        if first_result["phase"] != "done":
            phase = "cancelled" if first_result["phase"] == "cancelled" else "error"
            finish(phase, f"首个款号 {first} 未完成，后续款号未启动：{first_result['message']}")
            return
        if self.cancel_event.is_set():
            finish("cancelled", "任务已停止，首个款号结果已保留。")
            return
        remaining = codes[1:]
        if not remaining:
            finish("done", "批量任务已完成。")
            return

        page = primary_context.new_page()
        page.add_init_script(make_session_script(session_storage))
        open_business_page(page, operation)
        page.wait_for_timeout(1_800)
        if self._on_login_page(page, operation):
            self._wait_for_login(page, operation)
            if not same_page(page.url, operation["url"]):
                open_business_page(page, operation)
                page.wait_for_timeout(1_200)
        if not same_page(page.url, operation["url"]) or self._on_login_page(page, operation):
            finish("error", "首个款号已完成，但 OA 登录状态已失效，后续款号未启动。")
            return
        storage_state = primary_context.storage_state()
        session_storage = page.evaluate("""() => Object.fromEntries(
            Array.from({length: sessionStorage.length}, (_, index) => {
                const key = sessionStorage.key(index);
                return [key, sessionStorage.getItem(key)];
            })
        )""")
        session_script = make_session_script(session_storage)
        worker_count = min(MAX_BATCH_WORKERS, len(remaining))
        pending = queue.Queue()
        for code in remaining:
            pending.put(code)
        batch_abort = threading.Event()
        auth_failure = threading.Event()
        ready = threading.Event()
        startup_lock = threading.Lock()
        result_lock = threading.Lock()
        ready_count = [0]
        reasons = []

        def stop_all(reason: str, authentication: bool = False) -> None:
            with result_lock:
                if not reasons:
                    reasons.append(reason)
            if authentication:
                auth_failure.set()
            batch_abort.set()
            ready.set()

        def is_login_url(url: str) -> bool:
            return (urlparse(url).path.lower().endswith("/login.html") or
                    bool(operation.get("login_url") and same_page(url, operation["login_url"])))

        def worker(index: int) -> None:
            self.batch_local.abort = batch_abort
            browser = None
            try:
                with sync_playwright() as worker_playwright:
                    try:
                        # Only one worker can open and validate its browser at a time.
                        with startup_lock:
                            if batch_abort.is_set() or self.cancel_event.is_set():
                                return
                            try:
                                browser = worker_playwright.chromium.launch(channel="msedge", headless=False)
                            except Exception:
                                browser = worker_playwright.chromium.launch(headless=False)
                            context = browser.new_context(storage_state=storage_state)
                            context.add_init_script(session_script)
                            context.add_init_script(LOGIN_SUBMIT_GUARD)
                            install_noah_login_compatibility(context)
                            install_noah_print_compatibility(context)

                            def guard_login(route: Any) -> None:
                                request = route.request
                                if request.is_navigation_request() and is_login_url(request.url):
                                    stop_all("并发窗口的 OA 登录已失效，已停止后续款号。", True)
                                    route.abort()
                                elif request.is_navigation_request() and (batch_abort.is_set() or self.cancel_event.is_set()):
                                    route.abort()
                                else:
                                    route.fallback()

                            def guard_redirect(response: Any) -> None:
                                if response.request.is_navigation_request() and 300 <= response.status < 400:
                                    location = response.headers.get("location", "")
                                    if location and is_login_url(urljoin(response.url, location)):
                                        stop_all("并发窗口的 OA 登录已失效，已停止后续款号。", True)
                                        response.request.frame.page.close()

                            context.route("**/*", guard_login)
                            context.on("response", guard_redirect)
                            check_page = context.new_page()
                            open_business_page(check_page, operation)
                            check_page.wait_for_timeout(1_800)
                            if self._on_login_page(check_page, operation):
                                stop_all("并发窗口未继承 OA 登录状态，后续款号未启动。", True)
                                return
                            if not same_page(check_page.url, operation["url"]):
                                stop_all(f"第 {index} 个并发窗口未到达工艺单列表。")
                                return
                            ready_count[0] += 1
                            if ready_count[0] == worker_count:
                                ready.set()
                        while not ready.wait(0.2):
                            if self.cancel_event.is_set():
                                batch_abort.set()
                                ready.set()
                        while not batch_abort.is_set() and not self.cancel_event.is_set():
                            with result_lock:
                                if batch_abort.is_set() or self.cancel_event.is_set():
                                    break
                                try:
                                    code = pending.get_nowait()
                                except queue.Empty:
                                    break
                            job = dict(operation, style_code=code, batch_isolated=True,
                                       _batch_abort=batch_abort, _batch_auth_failure=auth_failure)
                            result = self._run(job, context, publish=False)
                            with result_lock:
                                results[code] = result
                                completed = len(results)
                            if auth_failure.is_set():
                                stop_all("并发窗口的 OA 登录已失效，已停止后续款号。", True)
                            elif not batch_abort.is_set() and not self.cancel_event.is_set():
                                self.update("reading", f"批量进度 {completed}/{len(codes)}；最近完成：{code}（{result['phase']}）",
                                            step=("convert" if operation["print_settings"].get("template_id") else "save_pdf") if result["phase"] == "done" else "query",
                                            batch_total=len(codes), batch_completed=completed)
                    except Exception as error:
                        if not batch_abort.is_set() and not self.cancel_event.is_set():
                            stop_all(f"第 {index} 个并发窗口启动失败：{error}")
                    finally:
                        if browser is not None:
                            browser.close()
            finally:
                del self.batch_local.abort

        self.update("opening", f"首个款号已完成，正在逐个验证 {worker_count} 个并发窗口…",
                    step="open", batch_total=len(codes), batch_completed=1)
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(worker, index) for index in range(1, worker_count + 1)]
            for future in as_completed(futures):
                future.result()

        if self.cancel_event.is_set():
            finish("cancelled", "批量任务已停止，已完成的结果已保留。")
        elif batch_abort.is_set():
            finish("error", reasons[0] if reasons else "并发任务已停止，已完成的结果已保留。")
        else:
            failures = sum(result["phase"] != "done" for result in results.values())
            finish("error" if failures else "done",
                   f"批量任务结束：成功 {len(results)-failures} 个，失败 {failures} 个。")

    def _run(self, operation: dict, context: Any, publish: bool = True) -> dict:
        final_phase = "error"
        final_message = "任务中断。"
        result = {}
        try:
            page = context.pages[0] if context.pages else context.new_page()
            open_business_page(page, operation, wait_until="commit" if is_process_sheet(operation) else "domcontentloaded")
            page.wait_for_timeout(1_800)
            self.check_cancelled()

            # A login page may redirect immediately when a session already exists.
            # Preserve that landing page instead of navigating back to login.
            if self._on_login_page(page, operation):
                self._wait_for_login(page, operation)
                if not same_page(page.url, operation["url"]):
                    self.update("opening", "登录完成，正在打开目标业务页面…", step="open")
                    open_business_page(page, operation)
                    page.wait_for_timeout(1_200)
                self.check_cancelled()
                if self._on_login_page(page, operation):
                    raise RuntimeError("返回业务页面后仍显示登录表单，请检查账号或登录状态。")

            if not same_page(page.url, operation["url"]):
                raise RuntimeError(f"未到达业务落地页，当前地址：{page.url}")

            if not operation.get("batch_isolated"):
                try:
                    preserve_login_session(context, operation)
                except Exception:
                    self.update("opening", "登录状态未能保存；关闭浏览器后可能需要重新登录。")

            if is_process_sheet(operation):
                result = self._print_process_sheets(context, page, operation)
                final_phase = "done"
                final_message = ("完整工艺单 PDF 已保存，辅料信息等待批量汇总。"
                                 if operation.get("_batch_trim") is not None and operation["print_settings"].get("template_id")
                                 else "完整工艺单 PDF 与辅料信息 XLSX 已保存，请检查文件内容。"
                                 if operation["print_settings"].get("template_id")
                                 else "完整工艺单 PDF 已保存；未选择模板，未生成辅料 XLSX。")
            else:
                self.update("reading", "正在读取业务页面主要内容…", step="read")
                result = {
                    "title": page.title(),
                    "url": page.url,
                    "content": main_page_text(page) or "页面没有可读取的文本内容。",
                }
                final_phase = "done"
                final_message = "业务页面已打开，主要内容已读取。"
        except Cancelled:
            final_phase = "cancelled"
            final_message = "任务已停止。"
        except Exception as error:
            final_message = f"执行失败：{error}"
        finally:
            if publish:
                self.update(final_phase, final_message, **result)
        return {"phase": final_phase, "message": final_message, **result}


PAGE_HTML = r'''<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>AIOA · 自动化工作台</title>
  <style>
    :root{--ink:#14213d;--muted:#68758c;--line:#e6eaf1;--blue:#2463e9;--blue-2:#164ac4;--pale:#eef4ff;--canvas:#f6f8fc;--white:#fff;--good:#0d996d;--warn:#b87513;--bad:#c63d4b;--shadow:0 16px 42px rgba(25,43,79,.055);font-family:Inter,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--ink)}
    *{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--canvas);min-width:320px}button,input{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.5}a{color:inherit;text-decoration:none}
    .shell{min-height:100vh;display:grid;grid-template-columns:238px minmax(0,1fr)}
    .sidebar{background:#101b34;color:#d5def0;display:flex;flex-direction:column;padding:31px 17px 22px;position:sticky;top:0;height:100vh}
    .brand{display:flex;align-items:center;gap:12px;padding:0 14px 37px;color:#fff}.brand-mark{width:37px;height:37px;display:grid;place-items:center;background:linear-gradient(145deg,#6da2ff,#2765eb);border-radius:11px;box-shadow:0 7px 18px rgba(39,101,235,.3)}.brand-name{font-size:20px;font-weight:760;letter-spacing:.01em}.brand-sub{font-size:10px;letter-spacing:.12em;color:#9aaccd;margin-top:1px}
    .nav-label{font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#7890b6;padding:0 15px;margin-bottom:13px}.nav-item{display:flex;align-items:center;gap:11px;padding:12px 14px;border-radius:9px;font-size:13px;font-weight:600;margin-bottom:4px;color:#aebdd8}.nav-item.active{background:rgba(82,126,214,.22);color:#fff}.nav-item svg{width:17px;height:17px}.side-bottom{margin-top:auto;border-top:1px solid rgba(255,255,255,.1);padding:22px 13px 0}.local-row{display:flex;align-items:center;gap:10px;color:#c2cde0;font-size:12px}.local-dot{width:7px;height:7px;border-radius:50%;background:#3ed9ac;box-shadow:0 0 0 4px rgba(62,217,172,.12)}.side-caption{color:#7185a7;font-size:11px;line-height:1.6;margin-top:11px}
    .main{min-width:0}.topbar{height:78px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 clamp(25px,4vw,64px)}.crumb{font-size:12px;color:#8a95a8}.crumb b{color:#344561;font-weight:640}.top-right{display:flex;align-items:center;gap:12px}.top-pill{border:1px solid #e4eaf5;border-radius:999px;padding:7px 11px;color:#64748c;font-size:11px;font-weight:650;background:#fbfcff}.avatar{height:32px;width:32px;border-radius:50%;background:#e7efff;display:grid;place-items:center;color:#3d69c2;font-size:12px;font-weight:760}
    .content{max-width:1480px;margin:auto;padding:38px clamp(25px,4vw,64px) 72px}.eyebrow{color:var(--blue);font-size:11px;font-weight:780;letter-spacing:.16em;text-transform:uppercase;margin:0 0 13px}.page-title{font-size:30px;letter-spacing:-.045em;line-height:1.25;margin:0;font-weight:760}.page-desc{margin:11px 0 30px;color:var(--muted);font-size:13px;line-height:1.7}
    .hero{min-height:170px;position:relative;overflow:hidden;border-radius:16px;background:linear-gradient(105deg,#1944a8 0%,#2565dd 55%,#5797f3 100%);color:#fff;padding:31px 36px;box-shadow:0 16px 32px rgba(40,95,202,.16);margin-bottom:28px}.hero:after{content:"";position:absolute;right:-72px;top:-142px;height:360px;width:360px;border:1px solid rgba(255,255,255,.25);border-radius:50%;box-shadow:0 0 0 37px rgba(255,255,255,.055),0 0 0 77px rgba(255,255,255,.04),0 0 0 117px rgba(255,255,255,.025)}.hero:before{content:"";position:absolute;right:183px;bottom:-95px;width:235px;height:235px;border:1px solid rgba(255,255,255,.1);border-radius:50%}.hero-kicker{position:relative;z-index:1;font-size:11px;font-weight:740;letter-spacing:.12em;color:#c6dcff}.hero h2{position:relative;z-index:1;margin:14px 0 8px;font-size:24px;font-weight:720;letter-spacing:-.035em}.hero p{position:relative;z-index:1;margin:0;color:#d8e6ff;font-size:12px;line-height:1.7;max-width:500px}
    .overview{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin-bottom:23px}.metric{background:#fff;border:1px solid var(--line);border-radius:13px;padding:17px 19px;display:flex;align-items:center;gap:13px}.metric-icon{height:37px;width:37px;border-radius:10px;background:#edf3ff;color:#416bd1;display:grid;place-items:center}.metric-icon svg{width:19px;height:19px}.metric-label{font-size:11px;color:#8995a8;margin-bottom:3px}.metric-value{font-size:15px;font-weight:720;color:#20304d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}
    .workspace{display:grid;grid-template-columns:minmax(300px,.94fr) minmax(380px,1.16fr);gap:20px;align-items:start}.panel{background:#fff;border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow);min-width:0}.panel-heading{padding:24px 24px 18px;display:flex;justify-content:space-between;align-items:start;gap:12px}.section-kicker{font-size:10px;font-weight:780;color:#8c9bb4;letter-spacing:.13em;text-transform:uppercase;margin:0 0 7px}.panel-heading h3{font-size:17px;letter-spacing:-.025em;margin:0}.panel-sub{font-size:11px;color:#8a96a9;margin-top:6px}.primary,.secondary,.ghost,.text-btn,.icon-btn{border:0;border-radius:9px;font-size:12px;font-weight:690;transition:.16s ease}.primary{background:var(--blue);color:#fff;padding:11px 15px;box-shadow:0 6px 12px rgba(36,99,233,.14)}.primary:hover:not(:disabled){background:var(--blue-2);transform:translateY(-1px)}.secondary{background:#eef3ff;color:#2854af;padding:11px 15px}.secondary:hover:not(:disabled){background:#e4edff}.ghost{background:#fff;color:#526480;border:1px solid #dfe6f0;padding:10px 14px}.ghost:hover:not(:disabled){background:#f6f8fc}.text-btn{background:none;color:#65748e;padding:7px 6px}.text-btn:hover{color:var(--blue)}.text-btn.danger:hover{color:var(--bad)}.icon-btn{height:34px;width:34px;background:#f4f7fd;color:#536789;display:grid;place-items:center}.icon-btn svg{width:15px;height:15px}.with-icon{display:inline-flex;align-items:center;gap:7px}.with-icon svg{width:14px;height:14px}
    .search-wrap{padding:0 24px 16px;position:relative}.search-wrap svg{position:absolute;left:38px;top:11px;width:15px;height:15px;color:#99a5b7}.search{width:100%;border:1px solid var(--line);border-radius:9px;background:#f9fbfe;padding:10px 12px 10px 37px;outline:none;font-size:12px;color:var(--ink)}.search:focus,.field:focus{border-color:#6d9af2;box-shadow:0 0 0 3px rgba(42,102,231,.1)}.search::placeholder,.field::placeholder{color:#a7b2c2}.operation-list{padding:0 12px 14px;max-height:455px;overflow:auto}.operation{width:100%;text-align:left;display:flex;align-items:center;gap:13px;padding:14px 12px;border:1px solid transparent;border-radius:10px;background:#fff;margin-bottom:5px}.operation:hover{background:#f6f9ff}.operation.selected{background:#eff5ff;border-color:#dce8ff}.op-icon{width:37px;height:37px;border-radius:10px;background:#e8efff;color:#3b6adb;display:grid;place-items:center;flex:none}.op-icon svg{width:19px;height:19px}.op-name{font-size:12px;font-weight:720;color:#233450;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.op-url{font-size:10px;color:#97a4b7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:4px}.op-copy{min-width:0;flex:1}.op-arrow{color:#9cadc9;font-size:19px;line-height:1}.operation.selected .op-arrow{color:var(--blue)}.empty{padding:34px 20px 43px;text-align:center;color:#95a1b2}.empty-icon{width:56px;height:56px;border-radius:16px;background:#edf3ff;display:grid;place-items:center;margin:0 auto 13px;color:#4d78d7}.empty h4{font-size:13px;color:#425471;margin:0 0 5px}.empty p{font-size:11px;line-height:1.65;margin:0}
    .detail{border-top:1px solid var(--line);padding:18px 24px 22px}.detail-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.detail-label{font-size:11px;color:#8895a7;font-weight:650}.detail-actions{display:flex;gap:6px}.selected-name{font-size:15px;font-weight:730;margin-top:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.selected-url{color:#8090a8;font-size:11px;line-height:1.5;word-break:break-all;margin:7px 0 0}.detail-none{font-size:11px;color:#9aa6b7;margin:10px 0 0}
    .run-head{padding-bottom:22px;border-bottom:1px solid var(--line)}.status-badge{display:inline-flex;gap:6px;align-items:center;background:#f3f6fb;color:#6a7a94;font-size:10px;font-weight:730;padding:7px 10px;border-radius:99px;white-space:nowrap}.status-badge:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}.status-badge.done{background:#e8f8f2;color:#0b9468}.status-badge.error{background:#fff0f0;color:#c63d4b}.status-badge.running{background:#eaf1ff;color:#2c66d7}.status-badge.login_required{background:#fff4df;color:#b7791b}
    .run-body{padding:22px 24px 24px}.timeline{display:flex;align-items:center;margin-bottom:24px}.step{display:flex;align-items:center;gap:8px;min-width:0;color:#abb7c8}.step-num{height:24px;width:24px;border-radius:50%;border:1px solid #d8e0ec;display:grid;place-items:center;font-size:10px;font-weight:780;flex:none}.step-label{font-size:11px;font-weight:680;white-space:nowrap}.step.active{color:#2a64da}.step.active .step-num{color:#fff;background:#2a64da;border-color:#2a64da}.step.complete{color:#11996e}.step.complete .step-num{color:#fff;background:#13a77b;border-color:#13a77b}.step-line{height:1px;flex:1;background:#e2e8f1;margin:0 12px;min-width:10px}.status-card{padding:18px;border:1px solid #e5ebf5;background:#f8faff;border-radius:11px;display:flex;gap:13px;align-items:start}.status-icon{width:32px;height:32px;background:#e9f0ff;color:#3668d3;border-radius:9px;display:grid;place-items:center;flex:none}.status-icon svg{width:17px;height:17px}.status-title{font-size:12px;font-weight:760;margin:1px 0 5px}.status-message{font-size:11px;color:#7a8ba4;line-height:1.65;word-break:break-word}.run-actions{display:flex;gap:9px;margin-top:20px}.run-actions button{min-width:103px}.fine-print{color:#9ca9ba;font-size:10px;line-height:1.6;margin:17px 0 0}
    .login-card{margin-top:18px;border:1px solid #f1dfbc;background:#fffbf4;border-radius:11px;padding:17px}.login-card h4{margin:0 0 5px;font-size:12px}.login-card p{margin:0 0 14px;font-size:11px;line-height:1.6;color:#92784d}.login-grid{display:grid;grid-template-columns:1fr 1fr;gap:11px}.field-label{display:block;color:#52627c;font-size:11px;font-weight:670;margin-bottom:7px}.field{width:100%;border:1px solid #dfe5ee;border-radius:8px;padding:10px 11px;outline:none;background:#fff;color:#243550;font-size:12px}.login-submit{margin-top:13px}
    .result{margin-top:22px;border-top:1px solid var(--line);padding-top:20px}.result-top{display:flex;justify-content:space-between;align-items:center}.result h4{font-size:12px;margin:0}.result-meta{color:#9aa8ba;font-size:10px}.result-title{font-size:13px;font-weight:720;margin:13px 0 5px}.result-url{font-size:10px;color:#7d8ca3;word-break:break-all;margin:0 0 12px}.result pre{white-space:pre-wrap;word-break:break-word;background:#f7f9fd;border:1px solid #e8edf5;border-radius:9px;padding:15px;font:11px/1.7 "Cascadia Code","Consolas",monospace;color:#344766;margin:0;max-height:330px;overflow:auto}
    .guide{margin-top:24px;padding:16px 18px;border:1px solid #e7ecf3;border-radius:11px;background:#fbfcff;display:flex;gap:11px;align-items:start;color:#5d7090}.guide svg{width:17px;height:17px;flex:none;margin-top:1px;color:#7191ca}.guide strong{display:block;font-size:11px;color:#435773;margin-bottom:4px}.guide p{font-size:10px;line-height:1.7;margin:0}
    .modal-backdrop{position:fixed;inset:0;background:rgba(12,25,51,.48);z-index:20;display:none;align-items:center;justify-content:center;padding:20px}.modal-backdrop.open{display:flex}.modal{width:min(100%,440px);background:#fff;border-radius:16px;box-shadow:0 28px 70px rgba(13,31,69,.22);padding:26px}.modal-top{display:flex;justify-content:space-between;align-items:center}.modal h3{margin:0;font-size:18px;letter-spacing:-.025em}.modal p{color:#7e8da3;font-size:11px;line-height:1.7;margin:8px 0 22px}.modal .field-label{margin-top:15px}.modal-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:24px}.toast{position:fixed;right:25px;bottom:24px;background:#172b4d;color:#fff;padding:12px 17px;border-radius:10px;box-shadow:0 15px 40px rgba(13,31,69,.2);font-size:11px;z-index:30;opacity:0;transform:translateY(8px);pointer-events:none;transition:.2s}.toast.show{opacity:1;transform:translateY(0)}
    .modal{max-height:calc(100vh - 40px);overflow:auto}
    @media(max-width:1060px){.shell{grid-template-columns:72px minmax(0,1fr)}.sidebar{padding:30px 11px}.brand{justify-content:center;padding:0 0 35px}.brand-copy,.nav-label,.nav-item span,.side-bottom{display:none}.nav-item{justify-content:center;padding:13px}.nav-item svg{width:20px;height:20px}}
    @media(max-width:800px){.workspace{grid-template-columns:1fr}.overview{grid-template-columns:repeat(3,minmax(0,1fr))}.content{padding:26px 20px 45px}.topbar{padding:0 20px}.hero{min-height:150px}}
    @media(max-width:560px){.shell{display:block}.sidebar{height:58px;position:static;flex-direction:row;align-items:center;padding:0 17px}.brand{padding:0}.brand-copy{display:block}.brand-sub{display:none}.nav-label,.nav-item,.side-bottom{display:none}.topbar{height:55px}.content{padding:24px 15px 40px}.page-title{font-size:25px}.hero{padding:24px}.hero h2{font-size:20px}.overview{gap:8px}.metric{padding:12px;gap:7px}.metric-icon{display:none}.metric-value{font-size:12px}.panel-heading,.run-body,.detail{padding-left:17px;padding-right:17px}.search-wrap{padding-left:17px;padding-right:17px}.search-wrap svg{left:30px}.login-grid{grid-template-columns:1fr}.top-pill{display:none}}
    /* Desktop workbench: quiet sidebar, conversation canvas, bottom composer. */
    :root{--ink:#242424;--muted:#767676;--line:#e8e8e8;--blue:#252525;--blue-2:#000;--pale:#f4f4f4;--canvas:#fff;--shadow:none}
    .shell{grid-template-columns:260px minmax(0,1fr)}
    .sidebar{background:#f9f9f9;color:#343434;padding:20px 12px;height:100dvh;overflow:auto;border-right:1px solid #ededed}
    .brand{padding:3px 12px 24px;color:#242424;gap:10px}.brand-mark{background:#252525;box-shadow:none;width:32px;height:32px;border-radius:10px}.brand-name{font-size:18px}.brand-sub{display:none}
    .nav-label,.nav-item,.hero,.overview,.eyebrow,.guide{display:none}
    .side-bottom{padding:18px 12px 0;border-top:1px solid #e8e8e8;margin-top:auto}.local-row{color:#666;font-size:11px}.side-caption{color:#999}.local-dot{background:#479d7a;box-shadow:none}
    .sidebar .panel{background:transparent;border:0;box-shadow:none;border-radius:0;flex:1;display:flex;flex-direction:column}
    .sidebar .panel-heading{padding:0 0 15px;display:block}.sidebar .panel-heading>div{display:none}
    #addButton{width:100%;justify-content:flex-start;background:transparent;color:#333;border:1px solid #e2e2e2;box-shadow:none;padding:12px;border-radius:10px;font-weight:550;font-size:13px}#addButton:hover{background:#ededed;transform:none}
    .search-wrap{padding:0 0 14px}.search-wrap svg{left:12px;color:#888}.search{background:#f0f0f0;border:0;font-size:12px;padding:11px 10px 11px 34px;border-radius:9px}
    .operation-list{padding:0;max-height:none;flex:1}.operation{padding:11px 10px;margin:2px 0;background:transparent;gap:9px;border-radius:8px}.operation:hover,.operation.selected{background:#eaeaea;border-color:transparent}.op-icon{background:transparent;color:#777;width:23px;height:27px}.op-icon svg{width:17px;height:17px}.op-name{font-size:13px;font-weight:500;color:#333}.op-url{font-size:10px;color:#999;max-width:180px}.op-arrow{display:none}
    .detail{padding:16px 8px 20px;border-top:1px solid #e7e7e7;margin-top:20px}.detail-label{color:#888;font-size:10px}.selected-name{font-size:12px;font-weight:600}.selected-url{font-size:10px;color:#999}.text-btn{font-size:11px;color:#777}.empty{padding:25px 10px}.empty-icon{background:#eee;color:#888;width:40px;height:40px}.empty h4{font-weight:500;color:#666}.empty p{color:#999}
    .topbar{height:64px;border-bottom:0;padding:0 30px;background:#fff}.crumb{font-size:16px;color:#777}.crumb b{font-weight:600;color:#303030}.top-pill{border:0;background:#f5f5f5;color:#888;font-size:10px}.avatar{background:#eaeaea;color:#444}
    .content{max-width:860px;padding:55px 34px 36px;min-height:calc(100dvh - 64px);display:flex;flex-direction:column}.page-title{font-size:30px;font-weight:600;text-align:center;letter-spacing:-.035em;margin-top:5vh}.page-desc{text-align:center;font-size:13px;color:#939393;margin:14px 0 45px}.workspace{display:block;flex:1}.workspace>.panel{border:0;box-shadow:none;border-radius:0;background:transparent}
    .run-head{border:0;padding:0 0 24px;align-items:center}.section-kicker{display:none}.run-head h3{font-size:14px;font-weight:600}.run-head .panel-sub{font-size:11px;color:#999}.run-body{padding:0;display:flex;flex-direction:column}.timeline{margin-bottom:25px}.step-label{font-size:11px;font-weight:500}.step.active{color:#333}.step.active .step-num{background:#333;border-color:#333}.step.complete{color:#4b7d67}.step.complete .step-num{background:#4b7d67;border-color:#4b7d67}.step-line{background:#e7e7e7}.step{color:#aaa}
    .status-card{background:transparent;border:0;padding:16px 0;gap:14px}.status-icon{background:#f1f1f1;color:#555;border-radius:50%;width:31px;height:31px}.status-title{font-size:13px;font-weight:600}.status-message{font-size:13px;line-height:1.85;color:#777}.status-badge{font-size:10px;background:#f3f3f3;color:#888}.status-badge.running{background:#f0f0f0;color:#555}
    .login-card{background:#fafafa;border:1px solid #e8e8e8;border-radius:16px;padding:22px}.login-card p{color:#888}.field{border-color:#dedede;border-radius:9px;font-size:13px}.field-label{color:#666}.field:focus,.search:focus{border-color:#aaa;box-shadow:0 0 0 3px #eee}.primary{background:#262626;box-shadow:none;border-radius:999px;font-size:12px}.primary:hover:not(:disabled){background:#000;transform:none}.ghost{border-color:#ddd;color:#555;border-radius:999px}.secondary{background:#eee;color:#333}
    .run-actions{order:8;position:sticky;bottom:20px;background:#f4f4f4;border:1px solid #e7e7e7;border-radius:23px;padding:17px 18px;margin-top:45px;align-items:center;box-shadow:0 5px 20px #00000005;flex-wrap:wrap}.composer-copy{flex:1;min-width:140px;font-size:13px;color:#777;line-height:1.6}.composer-copy small{display:block;color:#aaa;font-size:11px;margin-top:3px}.run-actions button{min-width:88px;padding:11px 16px}.fine-print{order:9;text-align:center;font-size:10px;color:#aaa;margin-top:12px}.result{order:5;border-top:1px solid #eee;margin-top:24px;padding-top:22px}.result pre{background:#fafafa;border-color:#e9e9e9;color:#444;border-radius:12px;max-height:500px;font-size:12px}.result-title{color:#333}.result-url{color:#999}
    .modal{border-radius:20px;padding:28px;box-shadow:0 24px 80px #0002}.modal-backdrop{background:#0005}.modal p{color:#888}.modal h3{font-weight:600}.icon-btn{background:#f4f4f4;color:#666}.toast{background:#292929}.sidebar-toggle{display:none;background:transparent;border:0;font-size:19px;color:#666;padding:8px;margin-right:10px}
    @media(max-width:1060px){.shell{grid-template-columns:238px minmax(0,1fr)}.sidebar{padding:20px 12px}.brand{justify-content:flex-start;padding:3px 12px 24px}.brand-copy,.side-bottom{display:block}.content{padding:35px 25px}.topbar{padding:0 22px}}
    @media(max-width:760px){.shell{display:block}.sidebar{position:fixed;left:0;top:0;width:260px;height:100dvh;z-index:15;display:flex;flex-direction:column;transform:translateX(-100%);transition:transform .18s;box-shadow:10px 0 35px #0001}.sidebar.sidebar-open{transform:translateX(0)}.sidebar-toggle{display:inline-block}.topbar{height:60px;padding:0 14px}.top-pill{display:none}.content{padding:24px 20px;min-height:calc(100dvh - 60px)}.page-title{font-size:25px;margin-top:5vh}.page-desc{font-size:12px;margin-bottom:35px}.run-body{padding:0}.run-head{padding:0 0 20px}.run-actions{bottom:10px;margin-top:30px}.login-grid{grid-template-columns:1fr}.brand{padding:3px 12px 24px}.brand-copy{display:block}}
    .theme-toggle{border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:999px;padding:8px 13px;font-size:12px;white-space:nowrap}.theme-toggle:hover{background:var(--pale)}
    html[data-theme="dark"]{color-scheme:dark;--ink:#ececec;--muted:#aaa;--line:#424242;--blue:#ececec;--blue-2:#fff;--pale:#353535;--canvas:#212121}
    html[data-theme="dark"] body,html[data-theme="dark"] .topbar{background:#212121;color:#ececec}
    html[data-theme="dark"] .sidebar{background:#171717;border-color:#303030;color:#ddd}
    html[data-theme="dark"] .brand,html[data-theme="dark"] .crumb b,html[data-theme="dark"] .op-name,html[data-theme="dark"] .selected-name,html[data-theme="dark"] .result-title,html[data-theme="dark"] .empty h4{color:#ececec}
    html[data-theme="dark"] .brand-mark{background:#3e3e3e}
    html[data-theme="dark"] #addButton{color:#ddd;border-color:#393939;background:transparent}
    html[data-theme="dark"] #addButton:hover,html[data-theme="dark"] .operation:hover,html[data-theme="dark"] .operation.selected{background:#303030}
    html[data-theme="dark"] .search,html[data-theme="dark"] .top-pill,html[data-theme="dark"] .avatar,html[data-theme="dark"] .status-icon,html[data-theme="dark"] .empty-icon,html[data-theme="dark"] .icon-btn{background:#303030;color:#bbb}
    html[data-theme="dark"] .side-bottom,html[data-theme="dark"] .detail,html[data-theme="dark"] .result{border-color:#373737}
    html[data-theme="dark"] .status-message,html[data-theme="dark"] .local-row,html[data-theme="dark"] .field-label,html[data-theme="dark"] .composer-copy{color:#bbb}
    html[data-theme="dark"] .status-badge,html[data-theme="dark"] .status-badge.running{background:#353535;color:#ccc}
    html[data-theme="dark"] .status-badge.done{background:#203e33;color:#8cdbb9}html[data-theme="dark"] .status-badge.error{background:#482b30;color:#ffa3ae}html[data-theme="dark"] .status-badge.login_required{background:#493d26;color:#edcb84}
    html[data-theme="dark"] .step.active{color:#eee}html[data-theme="dark"] .step.active .step-num{background:#ddd;border-color:#ddd;color:#222}html[data-theme="dark"] .step-num{border-color:#555}html[data-theme="dark"] .step-line{background:#424242}
    html[data-theme="dark"] .login-card,html[data-theme="dark"] .result pre{background:#272727;border-color:#424242;color:#ddd}
    html[data-theme="dark"] .field{background:#212121;border-color:#505050;color:#eee}html[data-theme="dark"] .field:focus,html[data-theme="dark"] .search:focus{border-color:#999;box-shadow:0 0 0 3px #ffffff12}
    html[data-theme="dark"] .run-actions{background:#303030;border-color:#424242;box-shadow:none}
    #printFields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 20px;margin-top:20px}
    #printFields .print-config{min-width:0}
    #printFields .field-label{display:block;margin:0 0 8px}
    #printFields .field{width:100%;min-width:0}
    #printFields .picker-row{display:flex;gap:8px;align-items:center}
    #printFields .picker-row .field{flex:1}
    #printFields .picker-row button{flex-shrink:0;white-space:nowrap}
    #printFields .panel-sub{grid-column:1/-1;margin:0}
    @media(max-width:680px){#printFields{grid-template-columns:1fr}}
    #printFields:not([hidden]) + .run-actions{position:static;margin-top:22px}
    .content{padding-top:22px}.page-title{margin:0;text-align:left;font-size:26px;line-height:1.3}.page-desc{margin:6px 0 22px;text-align:left;line-height:1.5}.run-head{padding-bottom:16px}.timeline{margin-bottom:18px;flex-wrap:wrap;row-gap:12px}.step-line{min-width:8px;margin:0 8px}
    @media(max-width:560px){.content{padding-top:18px}.page-title{font-size:23px}.page-desc{margin-bottom:18px}.step-label{white-space:normal}}
    html[data-theme="dark"] .primary{background:#ececec;color:#222}html[data-theme="dark"] .primary:hover:not(:disabled){background:#fff}
    html[data-theme="dark"] .ghost{background:transparent;color:#ddd;border-color:#555}html[data-theme="dark"] .ghost:hover:not(:disabled){background:#3a3a3a}
    html[data-theme="dark"] .modal{background:#292929;color:#eee}html[data-theme="dark"] .modal-backdrop{background:#0009}html[data-theme="dark"] .toast{background:#eee;color:#222}
    :focus-visible{outline:2px solid #888;outline-offset:3px}
  </style>
  <script>
    // Read preference before painting; this browser's choice survives server port changes.
    try{const saved=document.cookie.split('; ').find(item=>item.startsWith('aioa-theme='))?.split('=')[1];document.documentElement.dataset.theme=saved==='dark'||saved==='light'?saved:'dark'}catch{document.documentElement.dataset.theme='dark'}
  </script>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="brand"><div class="brand-mark"><svg width="23" height="23" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 12.2 9.1 5h4.2L8.2 12.2H4Zm6.5 6.8 5.1-7.2H20L14.9 19h-4.4Z" fill="white"/><path d="M10.9 12.2 15.9 5H20l-5.1 7.2h-4Z" fill="white" opacity=".64"/></svg></div><div class="brand-copy"><div class="brand-name">AIOA</div><div class="brand-sub">AUTOMATION STUDIO</div></div></div>
    <div class="nav-label">工作空间</div>
    <div class="nav-item active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/></svg><span>自动化工作台</span></div>
    <div class="side-bottom"><div class="local-row"><span class="local-dot"></span>本机服务运行中</div><div class="side-caption">任务与浏览器会话仅在这台电脑上运行。</div></div>
  </aside>
  <div class="main">
    <header class="topbar"><div class="crumb">工作空间&nbsp; / &nbsp;<b>自动化工作台</b></div><div class="top-right"><span class="top-pill">LOCAL WORKSPACE</span><span class="avatar">OA</span></div></header>
    <main class="content">
      <p class="eyebrow">WORKSPACE / 01</p>
      <h1 class="page-title">自动化工作台</h1>
      <p class="page-desc">集中管理 OA 业务入口，清晰掌握每一次执行的状态与结果。</p>
      <div class="hero"><div class="hero-kicker">WELCOME TO AIOA</div><h2>让日常业务，运行得更从容。</h2><p>选择业务操作，工作台将打开对应页面并处理登录；页面主要内容会呈现在这里。</p></div>
      <div class="overview">
        <div class="metric"><div class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h8M8 17h5"/></svg></div><div><div class="metric-label">已配置业务</div><div class="metric-value" id="countValue">—</div></div></div>
        <div class="metric"><div class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8"/><path d="m8.5 12 2.3 2.3 4.7-4.8"/></svg></div><div><div class="metric-label">当前任务</div><div class="metric-value" id="taskValue">待开始</div></div></div>
        <div class="metric"><div class="metric-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="6" width="16" height="13" rx="2"/><path d="M8 6V4m8 2V4M4 10h16"/></svg></div><div><div class="metric-label">运行方式</div><div class="metric-value">本机浏览器</div></div></div>
      </div>
      <div class="workspace">
        <section class="panel" aria-label="业务操作">
          <div class="panel-heading"><div><p class="section-kicker">BUSINESS OPERATIONS</p><h3>业务操作</h3><div class="panel-sub">选择业务开始运行</div></div></div>
          <div class="search-wrap"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/></svg><input class="search" id="searchInput" placeholder="搜索业务名称或地址" aria-label="搜索业务"></div>
          <div class="operation-list" id="operationList"></div>
          <div class="detail"><div class="detail-head"><span class="detail-label">当前选择</span></div><div id="selectionDetail" class="detail-none">尚未选择业务操作</div></div>
        </section>
        <section class="panel" aria-label="运行任务">
          <div class="panel-heading run-head"><div><p class="section-kicker">TASK EXECUTION</p><h3>运行任务</h3><div class="panel-sub">执行过程和页面结果实时更新</div></div><span class="status-badge" id="statusBadge">待开始</span></div>
          <div class="run-body">
            <div class="timeline" id="taskTimeline" aria-label="任务步骤"></div>
            <div class="status-card"><div class="status-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg></div><div><div class="status-title" id="statusTitle">准备就绪</div><div class="status-message" id="statusMessage">选择左侧业务操作后，即可开始。</div></div></div>
            <div class="login-card" id="loginCard" hidden><h4>完成 OA 登录</h4><p>在这里填写账号密码后，工具会将其填入 OA 页面。验证码请在 OA 浏览器中输入，再点击页面上的“登录”。</p><form id="loginForm"><div class="login-grid"><div><label class="field-label" for="username">用户名</label><input class="field" id="username" autocomplete="username" required></div><div><label class="field-label" for="password">密码</label><input class="field" id="password" type="password" autocomplete="current-password" required></div></div><button class="primary login-submit" type="submit" id="loginButton">填入 OA 页面</button></form></div>
            <div class="run-actions"><button class="primary" id="runButton" disabled>开始运行</button><button class="ghost" id="stopButton" hidden>停止任务</button></div>
            <p class="fine-print">密码不会保存；登录状态保存在本机浏览器配置中，OA 会话失效后需重新登录。</p>
            <div class="result" id="result" hidden><div class="result-top"><h4 id="resultHeading">页面主要内容</h4><span class="result-meta" id="resultTime"></span></div><div class="result-title" id="resultTitle"></div><p class="result-url" id="resultUrl"></p><pre id="resultContent"></pre></div>
          </div>
        </section>
      </div>
      <div class="guide"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg><div><strong>当前版本说明</strong><p>已支持业务入口管理、登录协助和页面内容读取。单号查询、列表筛选及勾选操作将在后续业务规则明确后接入。</p></div></div>
    </main>
  </div>
</div>
<div class="toast" id="toast" role="status"></div>
<script>
  const businessPanel = document.querySelector('[aria-label="业务操作"]');
  const sidebar = document.querySelector('.sidebar');
  sidebar.insertBefore(businessPanel, document.querySelector('.side-bottom'));
  document.querySelector('.hero').remove();
  document.querySelector('.page-title').textContent = '选择业务操作';
  document.querySelector('.page-desc').textContent = '选择左侧业务，查看任务内容并开始运行。';
  document.querySelector('.crumb').innerHTML = '<b>AIOA</b> <span style="font-size:12px;color:#aaa;margin-left:8px">工作台</span>';
  const menuButton = document.createElement('button');
  menuButton.className = 'sidebar-toggle'; menuButton.textContent = '☰'; menuButton.setAttribute('aria-label','展开或收起业务列表');
  menuButton.addEventListener('click',()=>sidebar.classList.toggle('sidebar-open'));
  document.querySelector('.crumb').prepend(menuButton);
  document.addEventListener('click',event=>{if(!sidebar.contains(event.target)&&!menuButton.contains(event.target))sidebar.classList.remove('sidebar-open')});
  const composerCopy=document.createElement('div');composerCopy.className='composer-copy';composerCopy.id='composerCopy';
  document.querySelector('.run-actions').prepend(composerCopy);
  const TOKEN = '__AIOA_TOKEN__';
  const themeButton=document.createElement('button');themeButton.type='button';themeButton.className='theme-toggle';
  function updateThemeButton(){const dark=document.documentElement.dataset.theme==='dark';themeButton.textContent=dark?'☀ 浅色':'☾ 深色';themeButton.setAttribute('aria-label',dark?'切换到浅色模式':'切换到深色模式')}
  themeButton.addEventListener('click',()=>{const next=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=next;document.cookie='aioa-theme='+next+'; Path=/; Max-Age=31536000; SameSite=Strict';updateThemeButton()});
  document.querySelector('.top-right').prepend(themeButton);updateThemeButton();
  let exiting=false;const exitButton=document.createElement('button');exitButton.type='button';exitButton.className='theme-toggle';exitButton.id='exitWorkbench';exitButton.textContent='退出工作台';document.querySelector('.top-right').append(exitButton);
  const $ = id => document.getElementById(id);
  let licenseState={valid:false,features:[]},licenseChecking=false,licenseAutoAttempted=false;
  const licenseButton=document.createElement('button');licenseButton.type='button';licenseButton.className='theme-toggle';licenseButton.id='licenseButton';licenseButton.textContent='授权管理';document.querySelector('.top-right').prepend(licenseButton);
  const licenseModal=document.createElement('div');licenseModal.className='modal-backdrop';licenseModal.id='licenseModal';licenseModal.innerHTML=`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="licenseTitle"><div class="modal-top"><h3 id="licenseTitle">软件授权</h3><button class="ghost" id="closeLicense" type="button">关闭</button></div><p id="licenseMessage" role="status"></p><label class="field-label">设备码</label><input id="licenseDevice" class="field" readonly><p id="licenseDetails" class="panel-sub"></p><div class="modal-actions" style="flex-wrap:wrap"><button id="exportRequest" class="ghost" type="button">导出设备申请</button><button id="checkLicense" class="ghost" type="button">联网校验</button><button id="importLicense" class="primary" type="button">导入授权文件</button></div><input id="licenseFile" type="file" accept=".license,application/json" hidden><p class="panel-sub">首次激活、工作台重启后及每隔 24 小时需要联网校时。当前任务可执行完成；到期后无法开始新任务。</p></section>`;document.body.append(licenseModal);
  const licensedModules=document.createElement('p');licensedModules.id='licensedModules';licensedModules.className='panel-sub';$('licenseDetails').after(licensedModules);
  function moduleAllowed(item){return licenseState.valid&&(licenseState.unrestricted||Array.isArray(licenseState.features)&&licenseState.features.includes(item?.module_id))}
  function renderLicense(){licenseButton.textContent=licenseState.valid?'授权有效':'授权管理';$('licenseMessage').textContent=licenseChecking?'正在联网校验时间…':licenseState.message||'尚未授权';$('licenseDevice').value=licenseState.device_id||'';$('licenseDetails').textContent=licenseState.expires_at?'授权到期：'+new Date(licenseState.expires_at).toLocaleString('zh-CN')+(licenseState.valid?' · 剩余 '+Math.ceil(licenseState.remaining_seconds/86400)+' 天':'')+(licenseState.customer?' · '+licenseState.customer:''):'';const features=licenseState.features||[];licensedModules.textContent='许可证模块范围：'+(features.length?features.map(id=>operations.find(item=>item.module_id===id)?.name||id).join('、'):'尚未授权');$('checkLicense').disabled=licenseChecking;$('importLicense').disabled=licenseChecking||runningPhases.has(currentTask.phase)}
  const renderRestrictedLicense=renderLicense;
  renderLicense=()=>{renderRestrictedLicense();if(licenseState.unrestricted){licenseButton.textContent='全部开放';licensedModules.textContent='模块范围：全部业务';$('licenseDevice').closest('section').querySelectorAll('.field-label,#licenseDevice,.modal-actions,.panel-sub:last-child').forEach(el=>el.hidden=true)}};
  licenseButton.onclick=()=>{renderLicense();licenseModal.classList.add('open')};$('closeLicense').onclick=()=>licenseModal.classList.remove('open');
  async function checkLicense(){if(licenseChecking)return;licenseChecking=true;renderLicense();try{const result=await api('/api/license/check','POST',{});licenseState=result.license}catch(error){licenseState={...licenseState,valid:false,message:error.message};toast(error.message)}finally{licenseChecking=false;renderLicense();renderTask()}}
  $('checkLicense').onclick=checkLicense;
  $('exportRequest').onclick=async()=>{try{const data=await api('/api/license/request','POST',{});const url=URL.createObjectURL(new Blob([JSON.stringify(data.request,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='AIOA-设备申请.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000)}catch(error){toast(error.message)}};
  $('importLicense').onclick=()=>$('licenseFile').click();$('licenseFile').onchange=async()=>{const file=$('licenseFile').files[0];if(!file)return;try{if(file.size>65536)throw Error('授权文件过大');licenseChecking=true;renderLicense();const result=await api('/api/license/import','POST',{document:await file.text()});licenseState=result.license;licenseAutoAttempted=false;toast('授权导入成功')}catch(error){toast(error.message)}finally{licenseChecking=false;$('licenseFile').value='';renderLicense();renderTask()}};
  const printFields=document.createElement('div');printFields.id='printFields';printFields.hidden=true;
  printFields.innerHTML=`<div class="print-config"><label class="field-label" for="styleCode">工艺单款号（单个）</label><input class="field" id="styleCode" maxlength="80" placeholder="例如：TTOW27A24B220B"><label class="field-label" for="styleCodesFile" style="margin-top:12px">或上传批量款号 XLSX（第一列每行一个，不含表头）</label><input class="field" id="styleCodesFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>
    <div class="print-config"><label class="field-label" for="templateHistory">辅料格式模板（XLSX，可选）</label><div class="picker-row"><select class="field" id="templateHistory"></select><button type="button" class="ghost" id="uploadTemplate">上传模板</button></div><input id="templateFile" type="file" hidden accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>
    <div class="print-config"><label class="field-label" for="originalDir">完整工艺单 PDF 保存目录</label><div class="picker-row"><input class="field" id="originalDir" list="originalHistory" placeholder="请选择文件夹或历史目录"><button type="button" class="ghost" id="chooseOriginalDir">选择文件夹</button></div><datalist id="originalHistory"></datalist></div>
    <div class="print-config"><label class="field-label" for="convertedDir">辅料转换 XLSX 保存目录（选择模板时必填）</label><div class="picker-row"><input class="field" id="convertedDir" list="convertedHistory" placeholder="请选择文件夹或历史目录"><button type="button" class="ghost" id="chooseConvertedDir">选择文件夹</button></div><datalist id="convertedHistory"></datalist></div>
    <p class="panel-sub">未选择模板时只保存 PDF；选择模板后将同时生成辅料 XLSX。</p>`;
  document.querySelector('.run-actions').before(printFields);
  let choosingDirectory=false;
  $('uploadTemplate').addEventListener('click',()=>$('templateFile').click());
  for(const [buttonId,fieldId] of [['chooseOriginalDir','originalDir'],['chooseConvertedDir','convertedDir']]){
    $(buttonId).addEventListener('click',async()=>{if(choosingDirectory)return;choosingDirectory=true;$(buttonId).textContent='选择中…';renderTask();try{const data=await api('/api/choose-directory','POST',{initial:$(fieldId).value});if(data.path)$(fieldId).value=data.path}catch(error){toast(error.message)}finally{choosingDirectory=false;$(buttonId).textContent='选择文件夹';renderTask()}});
  }
  function isPrintOperation(item){if(!item)return false;if(item.kind==='process_sheet')return true;try{const u=new URL(item.url);return u.pathname.toLowerCase().endsWith('/plm/stylemanage/newstylemanage.html')}catch{return false}}
  const settingsButton=document.createElement('button');settingsButton.className='theme-toggle';settingsButton.id='applicationSettings';settingsButton.textContent='应用配置';document.querySelector('.top-right').prepend(settingsButton);
  const settingsModal=document.createElement('div');settingsModal.className='modal-backdrop';
  settingsModal.innerHTML=`<div class="modal" role="dialog" aria-modal="true" aria-labelledby="settingsTitle"><div class="modal-top"><h3 id="settingsTitle">应用配置</h3><button type="button" class="icon-btn" id="closeSettings" aria-label="关闭配置">×</button></div><p>配置保存在当前电脑，后续任务自动使用。</p><form id="settingsForm"><label class="field-label" for="systemLoginUrl">登录页面 URL</label><input class="field" id="systemLoginUrl" type="url" placeholder="https://example.com/oa/login"><div class="modal-actions"><button type="submit" class="primary">保存登录设置</button></div></form><section style="margin-top:22px;padding-top:18px;border-top:1px solid var(--line)" aria-labelledby="supplierMappingTitle"><h4 id="supplierMappingTitle" style="margin:0 0 8px">供应商名称映射</h4><p class="panel-sub">可选 XLSX 文件，首个工作表第一列为供应商编号、第二列为供应商名称，可含表头。上传后长期保存，更新后用于下一次任务；未上传或编号未匹配时显示供应商编号。</p><p id="supplierMappingStatus" class="panel-sub" role="status"></p><input id="supplierMappingFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden><div class="modal-actions"><button id="clearSupplierMapping" type="button" class="ghost" disabled>清除映射</button><button id="uploadSupplierMapping" type="button" class="primary">上传映射表</button></div></section></div>`;
  document.body.append(settingsModal);
  let supplierMappingState={configured:false,count:0},supplierMappingBusy=false;
  const supplierMappingError=document.createElement('p');supplierMappingError.id='supplierMappingError';supplierMappingError.className='panel-sub';supplierMappingError.style.color='var(--bad)';supplierMappingError.setAttribute('role','alert');supplierMappingError.hidden=true;$('supplierMappingStatus').after(supplierMappingError);
  function renderSupplierMapping(){const active=runningPhases.has(currentTask.phase);$('uploadSupplierMapping').disabled=supplierMappingBusy||active;$('clearSupplierMapping').disabled=supplierMappingBusy||active||(!supplierMappingState.configured&&!supplierMappingState.error);$('uploadSupplierMapping').textContent=supplierMappingBusy?'处理中…':supplierMappingState.configured?'更新映射表':'上传映射表';$('supplierMappingStatus').textContent=supplierMappingState.error|| (supplierMappingState.configured?`${supplierMappingState.filename} · ${supplierMappingState.count} 条映射 · 更新于 ${new Date(supplierMappingState.uploaded_at).toLocaleString('zh-CN')}`:'尚未上传，导出的供应商字段将显示供应商编号。')}
  settingsButton.onclick=async()=>{try{const data=await api('/api/state');$('systemLoginUrl').value=data.settings.login_url||'';supplierMappingState=data.supplier_mapping||{configured:false,count:0};renderSupplierMapping();settingsModal.classList.add('open');$('systemLoginUrl').focus()}catch(error){toast(error.message)}};
  $('uploadSupplierMapping').onclick=()=>$('supplierMappingFile').click();
  $('supplierMappingFile').onchange=async()=>{const file=$('supplierMappingFile').files[0];if(!file)return;supplierMappingBusy=true;supplierMappingError.hidden=true;renderSupplierMapping();try{if(!file.name.toLowerCase().endsWith('.xlsx')||file.size>5*1024*1024)throw Error('请选择 5 MB 以内的 XLSX 映射表。');const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error('映射表读取失败'));reader.readAsDataURL(file)});const data=await api('/api/supplier-mapping','POST',{filename:file.name,content});supplierMappingState=data.supplier_mapping;toast('供应商映射已保存，后续任务自动使用')}catch(error){supplierMappingError.textContent=error.message;supplierMappingError.hidden=false;toast(error.message)}finally{supplierMappingBusy=false;$('supplierMappingFile').value='';renderSupplierMapping()}};
  $('clearSupplierMapping').onclick=async()=>{if(supplierMappingBusy)return;supplierMappingBusy=true;renderSupplierMapping();try{const data=await api('/api/supplier-mapping/clear','POST',{});supplierMappingState=data.supplier_mapping;supplierMappingError.hidden=true;toast('映射已清除，后续导出显示供应商编号')}catch(error){toast(error.message)}finally{supplierMappingBusy=false;renderSupplierMapping()}};
  $('closeSettings').onclick=()=>{settingsModal.classList.remove('open');settingsButton.focus()};
  settingsModal.addEventListener('click',event=>{if(event.target===settingsModal)$('closeSettings').click()});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&settingsModal.classList.contains('open'))$('closeSettings').click()});
  $('settingsForm').onsubmit=async event=>{event.preventDefault();try{await api('/api/settings','POST',{login_url:$('systemLoginUrl').value.trim()});$('closeSettings').click();await refresh();toast('系统设置已保存')}catch(error){toast(error.message)}};
  let operations = [], selectedName = '', currentTask = {phase:'idle'}, toastTimer, printSettingsInitialized = false, lastPrintSettingsKey = '';
  function syncPrintSettings(settings){if(!settings)return;const key=JSON.stringify([settings.original_history,settings.converted_history,settings.templates,settings.template_id]);if(printSettingsInitialized&&key===lastPrintSettingsKey)return;lastPrintSettingsKey=key;if(!printSettingsInitialized){$('originalDir').value=settings.original_dir||'';$('convertedDir').value=settings.converted_dir||'';printSettingsInitialized=true}for(const [name,values] of [['originalHistory',settings.original_history],['convertedHistory',settings.converted_history]]){const list=$(name);list.replaceChildren();for(const value of values||[]){const option=document.createElement('option');option.value=value;list.append(option)}}const select=$('templateHistory'),before=select.value,hadOptions=select.options.length>0;select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='不转换辅料（仅保存 PDF）';select.append(empty);for(const item of settings.templates||[]){const option=document.createElement('option');option.value=item.id;option.textContent=item.name+' · '+item.uploaded_at;select.append(option)}select.value=hadOptions&&[...select.options].some(o=>o.value===before)?before:(settings.template_id||'')}
  $('templateFile').addEventListener('change',async()=>{const file=$('templateFile').files[0];if(!file)return;if(file.size>5*1024*1024){toast('模板不能超过 5 MB');return}try{const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('模板读取失败'));reader.readAsDataURL(file)});const data=await api('/api/template','POST',{filename:file.name,content});await refresh();$('templateHistory').value=data.template.id;toast('模板已保存，下次默认使用')}catch(error){toast(error.message)}});
  const runningPhases = new Set(['opening','login_required','authenticating','reading','stopping']);
  const labels = {idle:'待开始',opening:'打开页面',login_required:'等待登录',authenticating:'验证中',reading:'读取中',stopping:'停止中',done:'已完成',error:'执行失败',cancelled:'已停止'};
  function toast(message){const node=$('toast');node.textContent=message;node.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.classList.remove('show'),3600)}
  async function api(path,method='GET',body){const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-AIOA-Token':TOKEN},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});let data;try{data=await response.json()}catch{data={error:'无法读取服务器响应。'}}if(!response.ok)throw new Error(data.error||'请求失败。');return data}
  function selected(){return operations.find(item=>item.name===selectedName)}
  function businessDescription(item){if(!item)return '选择左侧业务，查看任务内容并开始运行。';if(item.description)return item.description;return isPrintOperation(item)?'输入款号，查询并打印完整工艺单，再按模板生成辅料 XLSX。':'打开业务页面并读取主要内容。'}
  function canRun(item){if(!moduleAllowed(item)||licenseChecking||exiting||!item||runningPhases.has(currentTask.phase)||choosingDirectory)return false;if(!isPrintOperation(item))return true;const file=$('styleCodesFile').files[0],code=$('styleCode').value.trim(),template=$('templateHistory').value;if(file&&code)return false;const hasCodes=file?file.name.toLowerCase().endsWith('.xlsx'):/^[A-Za-z0-9_-]{1,80}$/.test(code);return hasCodes&&!!$('originalDir').value.trim()&&(!template||!!$('convertedDir').value.trim())}
  function renderTimeline(item){
    const steps=isPrintOperation(item)?[['open','打开业务页'],['query','查询款号'],['select','获取工艺单'],['save_pdf','保存完整 PDF']]:[['open','打开业务页'],['read','读取页面内容']];
    if(isPrintOperation(item)&&$('templateHistory').value){if(item.supplier_lookup_enabled)steps.push(['suppliers','查询辅料供应商']);steps.push(['convert','生成辅料 XLSX'])}
    const progress=item?.name===currentTask.operation?currentTask:{phase:'idle',step:'open',login_encountered:false};
    if(progress.login_encountered)steps.splice(1,0,['login','完成登录']);
    const index=Math.max(0,steps.findIndex(([key])=>key===(progress.step||'open')));
    const timeline=$('taskTimeline');timeline.replaceChildren();
    steps.forEach(([key,label],position)=>{if(position){const line=document.createElement('div');line.className='step-line';timeline.append(line)}const node=document.createElement('div');node.className='step'+(progress.phase==='done'||position<index?' complete':position===index?' active':'');node.dataset.step=key;const num=document.createElement('span');num.className='step-num';num.textContent=String(position+1);const title=document.createElement('span');title.className='step-label';title.textContent=label;node.append(num,title);timeline.append(node)});
  }
  function renderOperations(){
    const list=$('operationList');list.replaceChildren();const query=$('searchInput').value.trim().toLocaleLowerCase();const filtered=operations.filter(item=>(item.name+' '+item.url+' '+(item.login_url||'')).toLocaleLowerCase().includes(query));
    if(!filtered.length){const wrap=document.createElement('div');wrap.className='empty';wrap.innerHTML='<div class="empty-icon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 10h8M8 14h6"/></svg></div><h4>'+(operations.length?'没有找到匹配的业务':'还没有业务操作')+'</h4><p>'+(operations.length?'试试其他名称或地址':'暂无可用业务')+'</p>';list.append(wrap)}
    for(const item of filtered){const button=document.createElement('button');button.className='operation'+(item.name===selectedName?' selected':'');button.type='button';button.setAttribute('aria-pressed',item.name===selectedName?'true':'false');const icon=document.createElement('span');icon.className='op-icon';icon.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 6.5h16v11H4z"/><path d="M8 10h8M8 13.5h5"/></svg>';const copy=document.createElement('span');copy.className='op-copy';const name=document.createElement('div');name.className='op-name';name.textContent=item.name;const url=document.createElement('div');url.className='op-url';url.textContent=item.url;copy.append(name,url);const arrow=document.createElement('span');arrow.className='op-arrow';arrow.textContent='›';button.append(icon,copy,arrow);button.addEventListener('click',()=>{selectedName=item.name;renderOperations();renderDetail();renderTask()});list.append(button)}
    filtered.forEach((item,index)=>{if(licenseState.valid&&!moduleAllowed(item))list.children[index].querySelector('.op-name').textContent+=' · 未授权'});
    $('countValue').textContent=operations.length+' 项';
  }
  function renderDetail(){const item=selected();const box=$('selectionDetail');box.replaceChildren();if(!item){box.className='detail-none';box.textContent='尚未选择业务操作';return}box.className='';const n=document.createElement('div');n.className='selected-name';n.textContent=item.name;const u=document.createElement('p');u.className='selected-url';u.textContent=item.url;box.append(n,u);if(item.login_url){const login=document.createElement('p');login.className='selected-url';login.textContent='登录页：'+item.login_url;box.append(login)}}
  function renderTask(){
    renderSupplierMapping();
    exitButton.disabled=choosingDirectory||exiting;
    $('printFields').hidden=!isPrintOperation(selected());for(const id of ['styleCode','styleCodesFile','originalDir','convertedDir','templateHistory','templateFile','uploadTemplate'])$(id).disabled=runningPhases.has(currentTask.phase);
    for(const id of ['chooseOriginalDir','chooseConvertedDir'])$(id).disabled=choosingDirectory||runningPhases.has(currentTask.phase);
    $('convertedDir').disabled=runningPhases.has(currentTask.phase)||!$('templateHistory').value;
    $('chooseConvertedDir').disabled=choosingDirectory||runningPhases.has(currentTask.phase)||!$('templateHistory').value;
    const chosen=selected();$('composerCopy').textContent=chosen?chosen.name:'先选择一个业务操作';const hint=document.createElement('small');hint.textContent=businessDescription(chosen);$('composerCopy').append(hint);
    document.querySelector('.page-title').textContent=chosen?chosen.name:'选择业务操作';document.querySelector('.page-desc').textContent=businessDescription(chosen);
    const phase=currentTask.phase||'idle', active=runningPhases.has(phase), badge=$('statusBadge');badge.className='status-badge '+(phase==='done'?'done':phase==='error'?'error':phase==='login_required'?'login_required':active?'running':'');badge.textContent=labels[phase]||phase;$('taskValue').textContent=labels[phase]||'待开始';
    $('statusTitle').textContent=phase==='idle'?'准备就绪':(currentTask.operation||'业务操作')+' · '+(labels[phase]||phase);$('statusMessage').textContent=phase==='idle'?'选择左侧业务操作后，即可开始。':currentTask.message||'';
    if(phase==='idle'&&chosen&&licenseState.valid&&!moduleAllowed(chosen)){$('statusTitle').textContent='业务未授权';$('statusMessage').textContent='当前许可证未包含此业务模块，请联系管理员调整授权。'}
    $('runButton').disabled=!canRun(chosen)||!/^https?:\/\//.test(chosen?.url||'');$('runButton').textContent=phase==='done'||phase==='error'||phase==='cancelled'?'重新运行':'开始运行';$('stopButton').hidden=!active;$('stopButton').disabled=phase==='stopping';$('loginCard').hidden=phase!=='login_required';
    renderTimeline(active?(operations.find(item=>item.name===currentTask.operation)||chosen):chosen);
    const hasResult=(phase==='done'||(['error','cancelled'].includes(phase)&&currentTask.batch_total>1))&&currentTask.content;$('result').hidden=!hasResult;if(hasResult){$('resultHeading').textContent=currentTask.batch_total>1?'批量执行结果':'页面主要内容';$('resultTitle').textContent=currentTask.title||'OA 业务页面';$('resultUrl').textContent=currentTask.url||'';$('resultContent').textContent=currentTask.content;$('resultTime').textContent=currentTask.updated_at||''}
  }
  async function refresh(){try{const data=await api('/api/state');operations=data.operations||[];currentTask=data.task||{phase:'idle'};if(!supplierMappingBusy)supplierMappingState=data.supplier_mapping||{configured:false,count:0};if(!licenseChecking){licenseState=data.license||{valid:false};renderLicense();if(licenseState.valid)licenseAutoAttempted=false;else if(licenseState.code==='time_check_required'&&!licenseAutoAttempted){licenseAutoAttempted=true;checkLicense()}}syncPrintSettings(data.print_settings);if(selectedName&&!selected())selectedName='';if(!selectedName&&operations.length)selectedName=operations[0].name;renderOperations();renderDetail();renderTask();}catch(error){$('statusMessage').textContent='本地服务连接失败：'+error.message}}
  $('searchInput').addEventListener('input',renderOperations);
  $('styleCode').addEventListener('input',()=>{if($('styleCode').value.trim())$('styleCodesFile').value=''});
  $('styleCodesFile').addEventListener('change',()=>{if($('styleCodesFile').files.length)$('styleCode').value=''});
  for(const id of ['styleCode','styleCodesFile','originalDir','convertedDir','templateHistory']){for(const event of ['input','change'])$(id).addEventListener(event,renderTask)}
  $('runButton').addEventListener('click',async()=>{const item=selected();if(!item)return;const style_code=$('styleCode').value.trim().toUpperCase();if(isPrintOperation(item)&&!canRun(item)){toast('请填写单个款号或选择 XLSX 文件，并完成其余运行配置');return}try{if(isPrintOperation(item))await api('/api/print-settings','POST',{original_dir:$('originalDir').value.trim(),converted_dir:$('convertedDir').value.trim(),template_id:$('templateHistory').value});const body={name:item.name,style_code};const file=$('styleCodesFile').files[0];if(isPrintOperation(item)&&file){body.style_codes_file={filename:file.name,content:await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('批量款号文件读取失败'));reader.readAsDataURL(file)})}}await api('/api/run','POST',body);await refresh()}catch(error){toast(error.message)}});
  $('stopButton').addEventListener('click',async()=>{try{await api('/api/stop','POST',{});await refresh()}catch(error){toast(error.message)}});
  $('loginForm').addEventListener('submit',async event=>{event.preventDefault();const username=$('username').value,password=$('password').value;try{await api('/api/credentials','POST',{username,password});$('password').value='';await refresh();toast('登录信息已填入 OA 页面')}catch(error){toast(error.message)}});
  const refreshTimer=setInterval(refresh,1300);
  exitButton.addEventListener('click',async()=>{if(runningPhases.has(currentTask.phase)&&!confirm('当前任务正在运行，退出会停止任务。确定退出工作台吗？'))return;exiting=true;exitButton.disabled=true;exitButton.textContent='正在退出…';try{await api('/api/shutdown','POST',{});clearInterval(refreshTimer);$('statusMessage').textContent='正在停止任务并关闭自动化浏览器，请稍候…';$('runButton').disabled=true;$('stopButton').disabled=true;const timer=setInterval(async()=>{try{await api('/api/state')}catch(_){clearInterval(timer);$('statusMessage').textContent='工作台服务已停止，可以关闭此页面。';exitButton.textContent='工作台已退出'}},1000)}catch(error){exiting=false;exitButton.disabled=false;exitButton.textContent='退出工作台';toast(error.message)}});
  refresh();
</script>
</body>
</html>'''


CONTROLLER = AutomationController()
ACCESS_TOKEN = secrets.token_urlsafe(32)
LICENSES = LicenseManager(data_dir, unrestricted=True)


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "AIOA/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        if content_type == "application/json":
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _trusted_host(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _authorized(self) -> bool:
        return (
            self._trusted_host()
            and self.headers.get("X-AIOA-Token") == ACCESS_TOKEN
            and self.headers.get("Origin") in (None, f"http://127.0.0.1:{self.server.server_port}")
        )

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if self.path == "/api/license/import":
            limit = 256 * 1024
        elif self.path == "/api/template":
            limit = MAX_TEMPLATE_BYTES * 2
        elif self.path == "/api/supplier-mapping":
            limit = MAX_MAPPING_BYTES * 2
        elif self.path == "/api/run":
            limit = MAX_BATCH_FILE_BYTES * 2 + MAX_BODY_BYTES
        else:
            limit = MAX_BODY_BYTES
        if length < 1 or length > limit:
            raise ValueError("请求内容大小无效。")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求格式无效。")
        return value

    def do_GET(self) -> None:
        if not self._trusted_host():
            self._send(403, {"error": "访问被拒绝。"})
        elif self.path == "/":
            self._send(200, PAGE_HTML.replace("__AIOA_TOKEN__", ACCESS_TOKEN), "text/html")
        elif self.path == "/api/state" and self._authorized():
            with CONFIG_LOCK:
                settings = load_settings()
                operations = business_operations(settings)
                print_settings = load_print_settings()
            self._send(200, {"operations": operations, "settings": settings, "print_settings": print_settings, "task": CONTROLLER.snapshot(), "license": LICENSES.status(),
                             "supplier_mapping": supplier_mapping_status(data_dir())})
        else:
            self._send(404, {"error": "页面不存在。"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(403, {"error": "访问被拒绝。"})
            return
        try:
            body = self._body()
            if self.path == "/api/license/request":
                self._send(200, {"request": LICENSES.request()})
            elif self.path == "/api/license/check":
                LICENSES.require_valid(force=True)
                self._send(200, {"license": LICENSES.status()})
            elif self.path == "/api/license/import":
                if CONTROLLER.task_active:
                    raise ValueError("任务运行期间不能更换授权文件。")
                content = body.get("document")
                if not isinstance(content, str):
                    raise ValueError("授权文件格式无效。")
                LICENSES.install(content.encode("utf-8"))
                self._send(200, {"license": LICENSES.status()})
            elif self.path in {"/api/supplier-mapping", "/api/supplier-mapping/clear"}:
                if CONTROLLER.task_active:
                    raise ValueError("任务运行期间不能更新供应商映射表，请等待任务完成。")
                with CONFIG_LOCK:
                    if self.path.endswith("/clear"):
                        (data_dir() / "supplier-names.json").unlink(missing_ok=True)
                        status = supplier_mapping_status(data_dir())
                    else:
                        status = save_supplier_mapping(data_dir(), body.get("filename"), body.get("content"))
                self._send(200, {"supplier_mapping": status})
            elif self.path == "/api/choose-directory":
                self._send(200, {"path": choose_directory(str(body.get("initial", "")))})
            elif self.path == "/api/print-settings":
                with CONFIG_LOCK:
                    settings = load_print_settings()
                    template_id = str(body.get("template_id", "")).strip()
                    if template_id and not any(item["id"] == template_id for item in settings["templates"]):
                        raise ValueError("模板不存在，请重新上传。")
                    converted_dir = str(body.get("converted_dir", "")).strip()
                    if template_id and not converted_dir:
                        raise ValueError("选择辅料模板后，请设置辅料 XLSX 保存目录。")
                    remember_directory(settings, "original_dir", str(body.get("original_dir", "")))
                    if template_id:
                        remember_directory(settings, "converted_dir", converted_dir)
                    settings["template_id"] = template_id
                    save_print_settings(settings)
                self._send(200, {"ok": True})
            elif self.path == "/api/template":
                template = store_template(str(body.get("filename", "")), str(body.get("content", "")))
                with CONFIG_LOCK:
                    settings = load_print_settings()
                    settings["templates"] = [template] + [item for item in settings["templates"] if item["id"] != template["id"]][:9]
                    settings["template_id"] = template["id"]
                    save_print_settings(settings)
                self._send(200, {"ok": True, "template": template})
            elif self.path == "/api/settings":
                value = str(body.get("login_url", "")).strip()
                settings = {"login_url": normalize_url(value) if value else ""}
                with CONFIG_LOCK:
                    target = data_dir() / "settings.json"
                    temporary = target.with_suffix(".tmp")
                    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temporary, target)
                self._send(200, {"ok": True})
            elif self.path == "/api/operations":
                self._send(403, {"error": "业务由系统内置，不能修改。"})
            elif self.path == "/api/run":
                name = str(body.get("name", ""))
                with CONFIG_LOCK:
                    settings = load_settings()
                    operation = next((dict(item, login_url=settings["login_url"]) for item in business_operations(settings) if item["name"] == name), None)
                if operation is None:
                    raise ValueError("业务操作不存在，请刷新页面。")
                if urlparse(operation["url"]).scheme not in {"http", "https"}:
                    raise ValueError("请先配置登录地址，再执行任务。")
                if is_process_sheet(operation):
                    batch_file = body.get("style_codes_file")
                    style_code = str(body.get("style_code", "")).strip().upper()
                    if batch_file is not None and style_code:
                        raise ValueError("单个款号和批量 XLSX 不能同时提交，请选择其中一种。")
                    if batch_file is not None:
                        if not isinstance(batch_file, dict):
                            raise ValueError("批量款号文件数据无效。")
                        codes = read_style_codes(str(batch_file.get("filename", "")),
                                                 str(batch_file.get("content", "")))
                    else:
                        if not re.fullmatch(r"[A-Z0-9_-]{1,80}", style_code):
                            raise ValueError("请输入有效款号或选择批量 XLSX 文件。")
                        codes = [style_code]
                    operation["style_codes"] = codes
                    if len(codes) == 1:
                        operation["style_code"] = codes[0]
                    with CONFIG_LOCK:
                        print_settings = load_print_settings()
                    if not print_settings["original_dir"]:
                        raise ValueError("请先设置完整工艺单 PDF 保存目录。")
                    if print_settings["template_id"] and not print_settings["converted_dir"]:
                        raise ValueError("选择辅料模板后，请设置辅料 XLSX 保存目录。")
                    operation["print_settings"] = print_settings
                CONTROLLER.start(operation)
                self._send(200, {"ok": True})
            elif self.path == "/api/credentials":
                CONTROLLER.submit_credentials(str(body.get("username", "")), str(body.get("password", "")))
                self._send(200, {"ok": True})
            elif self.path == "/api/stop":
                CONTROLLER.stop()
                self._send(200, {"ok": True})
            elif self.path == "/api/shutdown":
                def finish_shutdown() -> None:
                    CONTROLLER.shutdown()
                    self.server.shutdown()
                self._send(200, {"ok": True})
                threading.Thread(target=finish_shutdown, daemon=True).start()
            else:
                self._send(404, {"error": "接口不存在。"})
        except (ValueError, json.JSONDecodeError) as error:
            self._send(400, {"error": str(error)})
        except Exception as error:
            self._send(500, {"error": f"服务器错误：{error}"})

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._send(403, {"error": "访问被拒绝。"})
        elif self.path == "/api/operations":
            self._send(403, {"error": "业务由系统内置，不能删除。"})
        else:
            self._send(404, {"error": "接口不存在。"})


def run() -> int:
    configure_console()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", WORKBENCH_PORT), LocalHandler)
    except OSError as error:
        print(f"工作台端口 {WORKBENCH_PORT} 无法使用，请先关闭旧工作台后重试：{error}", file=sys.stderr)
        return 1
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"AIOA 工作台已启动：{address}")
    print("关闭此窗口可退出；也可在这里按 Ctrl+C。")
    threading.Timer(0.8, lambda: webbrowser.open_new_tab(address)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        CONTROLLER.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        from license_keys import PUBLIC_KEYS
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from playwright._impl._driver import compute_driver_executable
        from openpyxl import Workbook
        if not PUBLIC_KEYS:
            raise SystemExit("No embedded public keys configured.")
        for public in PUBLIC_KEYS.values():
            Ed25519PublicKey.from_public_bytes(public)
        node, driver = compute_driver_executable()
        if not Path(node).is_file() or not Path(driver).is_file():
            raise SystemExit("Bundled Playwright driver is missing.")
        workbook = Workbook()
        workbook.close()
        print("AIOA compiled self-test OK")
    elif sys.argv[1:] == ["--browser-smoke"]:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                page.set_content("<h1>AIOA</h1>")
                assert page.locator("h1").inner_text() == "AIOA"
            finally:
                browser.close()
        print("AIOA compiled browser smoke test OK")
    elif sys.argv[1:]:
        raise SystemExit("Unsupported command line arguments.")
    else:
        raise SystemExit(run())
