#!/usr/bin/env python3
"""Local OA automation workbench. Run with Python, then use its browser UI."""

import base64
from contextlib import contextmanager
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
import tempfile
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
from pptx_service import (ExtractionCancelled, choose_files, executable as pptx_executable,
                          invoke as invoke_pptx, prepare_request as prepare_pptx_request,
                          validate_file as validate_pptx_file)
from pptx_service import load_settings as load_pptx_settings, save_settings as save_pptx_settings
from pptx_service import validate_selected_paths
from ui_resources import render_page
from template_builder import collect_labels, create_template
from pptx_suppliers import extract_with_suppliers as extract_pptx_with_suppliers, fill_suppliers
from update_manager import UpdateManager
import pptx_service
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
                 supplier_lookup_enabled=PROFILE["supplier_lookup"]["enabled"]),
            {"name":"PPTX 面辅料提取", "kind":"pptx_extract", "module_id":"pptx_material_extract",
             "url":"本地文件", "description":"提取面辅料并汇总到 XLSX，模板有供应商列时自动查询并映射供应商名称。",
             "supplier_lookup_enabled": PROFILE["supplier_lookup"]["enabled"]},
            {"name":"制作模板", "kind":"template_builder", "module_id":"template_builder",
             "url":"本地文件", "description":"从 PPTX 或 XLSX 识别候选字段，编辑列名后下载空白提取模板。"}]


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
        self.local_worker = None
        self.task_active = False
        self.updating = False
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
            worker = self.local_worker if self.state.get("operation") == "PPTX 面辅料提取" else self.worker
            if self.task_active and worker is not None and not worker.is_alive():
                self.task_active = False
                self.state.update(phase="error", message="任务工作进程已结束，可重新选择文件并运行。")
            return dict(self.state, maintenance=self.updating, task_active=self.task_active)

    def reset(self) -> dict:
        with self.lock:
            if self.task_active or self.updating or self.shutdown_event.is_set():
                raise ValueError("请先停止任务并等待结束，再重置任务。")
            self.cancel_event.clear()
            self.messages = queue.Queue()
            self.state = {"phase": "idle", "step": "open", "login_encountered": False,
                          "message": "任务已重置，请重新选择文件或填写款号。", "operation": "",
                          "title": "", "url": "", "content": "", "updated_at": ""}
        return self.snapshot()

    @contextmanager
    def maintenance(self):
        with self.lock:
            if self.task_active or self.updating or self.shutdown_event.is_set():
                raise ValueError("请等待任务或其他更新操作结束后再更新软件。")
            self.updating = True
        try:
            yield
        finally:
            with self.lock:
                self.updating = False

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
        if operation.get("kind") == "template_builder":
            raise ValueError("请在制作模板页面选择文件并下载模板。")
        module_id = operation.get("module_id")
        if not isinstance(module_id, str) or not module_id.strip():
            raise ValueError("该业务尚未配置授权模块，请联系管理员。")
        LICENSES.require_valid(feature=module_id)
        if operation.get("kind") == "pptx_extract":
            self._start_pptx(operation)
            return
        with self.lock:
            if self.shutdown_event.is_set():
                raise ValueError("工作台正在退出，无法启动新任务。")
            if self.task_active or self.updating:
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

    def _start_pptx(self, operation: dict) -> None:
        request = prepare_pptx_request(operation["request"])
        with self.lock:
            if self.shutdown_event.is_set() or self.task_active or self.updating:
                raise ValueError("已有任务正在运行或工作台正在退出。")
            binary = pptx_executable()
            self.messages = queue.Queue()
            self.cancel_event.clear()
            self.task_active = True
            self.state = {"phase": "running", "step": "extract", "login_encountered": False,
                          "operation": operation["name"], "message": "正在读取 PPTX…",
                          "title": "", "url": "", "content": "", "batch_total": len(request["inputs"]),
                          "batch_completed": 0, "updated_at": datetime.now().strftime("%H:%M:%S")}
            self.local_worker = threading.Thread(target=self._run_pptx, args=(request, binary), daemon=True)
            self.local_worker.start()

    def _run_pptx(self, request: dict, binary: Path) -> None:
        completed, messages = 0, []
        def publish(event):
            nonlocal completed
            if event.get("type") == "progress":
                self.update("running", f"正在提取 {event['current']}/{event['total']}：{Path(event['input']).name}", step="extract")
            elif event.get("type") == "file":
                completed += 1
                messages.extend(event.get("warnings") or [])
                if not event.get("ok"):
                    messages.append(f"{Path(event['input']).name}：{event.get('error', '失败')}")
                self.update("running", "正在汇总提取数据…", step="extract", batch_completed=completed,
                            content="\n".join(messages[-MAX_OUTPUT_LINES:]))
        try:
            result = extract_pptx_with_suppliers(request, self.cancel_event, publish, binary,
                                               self._pptx_supplier_stage,
                                               enabled=PROFILE["supplier_lookup"]["enabled"])
            if result.get("supplier_missing"):
                messages.append(f"{result['supplier_missing']} 行辅料未查询到供应商，对应单元格留空。")
            if "supplier_queries" in result:
                messages.append(f"供应商补全：查询 {result['supplier_queries']} 个不同辅料编号。")
            self.update("done", f"提取完成：成功 {result['success']} 个，失败 {result['failed']} 个。",
                        step="export", output_path=result["output"],
                        content="\n".join([f"已保存：{result['output']}"] + messages[-MAX_OUTPUT_LINES:]))
        except (ExtractionCancelled, Cancelled):
            self.update("cancelled", "提取已停止。", step="extract")
        except Exception as error:
            self.update("error", f"提取失败：{error}", step="extract")
        finally:
            with self.lock:
                self.task_active = False

    def _pptx_supplier_stage(self, path: Path, code_column: int, columns: dict) -> dict:
        with CONFIG_LOCK:
            settings = load_settings()
            mapping = load_supplier_mapping(data_dir())["entries"]
        if not settings.get("login_url"):
            raise ValueError("供应商补全需要 OA 登录地址，请先在应用配置中设置。")
        done, outcome = threading.Event(), {}
        operation = {"kind": "pptx_suppliers", "url": business_url(settings),
                     "login_url": settings["login_url"], "supplier_name_mapping": mapping,
                     "_supplier_done": done, "_supplier_outcome": outcome,
                     "_supplier_action": lambda lookup: fill_suppliers(path, code_column, columns, lookup, self.cancel_event)}
        self.update("reading", "正在查询并映射辅料供应商…", step="suppliers")
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._worker_loop, daemon=True)
                self.worker.start()
            self.jobs.put(operation)
        while not done.wait(0.1):
            if not self.worker.is_alive():
                done.set()
                if self.cancel_event.is_set() or self.shutdown_event.is_set():
                    raise ExtractionCancelled()
                raise RuntimeError("供应商查询服务未能启动，请重试。")
        if "error" in outcome:
            raise outcome["error"]
        if "result" not in outcome:
            if self.cancel_event.is_set() or self.shutdown_event.is_set():
                raise ExtractionCancelled()
            raise RuntimeError("供应商查询未完成，请重新运行任务。")
        return outcome["result"]

    def _finish_pptx_suppliers(self, context: Any, operation: dict) -> dict:
        page = next((item for item in context.pages if not item.is_closed()), None) or context.new_page()
        options = self._supplier_export_options(context, page, operation)
        if "supplier_lookup" not in options:
            raise ValueError("供应商查询未启用。")
        lookup = options["supplier_lookup"]
        login_retried = False

        def query(code):
            nonlocal login_retried
            try:
                return lookup(code)
            except RuntimeError as error:
                message = str(error)
                if login_retried or not any(text in message for text in ("没有返回 JSON", "未登录", "请先登录", "登录失效")):
                    raise
                login_retried = True
                page.goto(operation["login_url"], wait_until="commit", timeout=60_000)
                self._wait_for_login(page, operation)
                return lookup(code)

        return operation["_supplier_action"](query)

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
            self.state.update(phase="stopping", message="正在停止任务…",
                              updated_at=datetime.now().strftime("%H:%M:%S"))
        self.messages.put(("cancel",))

    def shutdown(self) -> None:
        with self.lock:
            self.shutdown_event.set()
            self.cancel_event.set()
        self.messages.put(("cancel",))
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join()
        if self.local_worker and self.local_worker is not threading.current_thread():
            self.local_worker.join()

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
                    if operation.get("kind") == "pptx_suppliers" and operation["_supplier_done"].is_set():
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
                        if operation.get("kind") == "pptx_suppliers":
                            operation["_supplier_outcome"]["result"] = self._finish_pptx_suppliers(context, operation)
                        elif len(operation.get("style_codes", [])) > 1:
                            self._run_batch(operation, context, playwright)
                        else:
                            if operation.get("style_codes"):
                                operation["style_code"] = operation["style_codes"][0]
                            self._run(operation, context)
                    except Exception as error:
                        if operation.get("kind") == "pptx_suppliers":
                            operation["_supplier_outcome"]["error"] = error
                        else:
                            self.update("error", f"执行失败：{error}")
                        if context is not None and operation.get("kind") != "pptx_suppliers":
                            try:
                                context.close()
                            except Exception:
                                pass
                            context = None
                    finally:
                        if operation.get("kind") == "pptx_suppliers":
                            operation["_supplier_done"].set()
                        else:
                            with self.lock:
                                self.task_active = False
        except Exception as error:
            self.update("error", f"浏览器服务启动失败：{error}")
            with self.lock:
                if not self.local_worker or not self.local_worker.is_alive():
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





CONTROLLER = AutomationController()
ACCESS_TOKEN = secrets.token_urlsafe(32)
LICENSES = LicenseManager(data_dir, unrestricted=True)
UPDATES = UpdateManager(lambda: data_dir())
pptx_service.COMPONENT_RESOLVER = lambda path: UPDATES.component(path)


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
        elif self.path in {"/api/pptx/run", "/api/pptx/template", "/api/pptx/settings", "/api/template-builder/download", "/api/pptx/files", "/api/template-builder/source"}:
            limit = 4 * 1024 * 1024
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
        elif self.path in {"/", "/updates/recovery"}:
            try:
                page = (render_page(ACCESS_TOKEN) if self.path == "/updates/recovery"
                        else UPDATES.page(ACCESS_TOKEN))
                self._send(200, page, "text/html")
            except ValueError as error:
                self._send(503, {"error": str(error)})
        elif self.path == "/api/updates/status" and self._authorized():
            self._send(200, UPDATES.status())
        elif self.path == "/api/state" and self._authorized():
            with CONFIG_LOCK:
                settings = load_settings()
                operations = business_operations(settings)
                print_settings = load_print_settings()
            self._send(200, {"operations": operations, "settings": settings, "print_settings": print_settings, "task": CONTROLLER.snapshot(), "license": LICENSES.status(),
                             "supplier_mapping": supplier_mapping_status(data_dir())})
        elif self.path == "/api/pptx/settings" and self._authorized():
            try:
                self._send(200, load_pptx_settings(data_dir()))
            except ValueError as error:
                self._send(400, {"error":str(error)})
        else:
            self._send(404, {"error": "页面不存在。"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(403, {"error": "访问被拒绝。"})
            return
        try:
            if self.path == "/api/updates/install":
                self._install_update()
                return
            body = self._body()
            if self.path == "/api/template-builder/source":
                LICENSES.require_valid(feature="template_builder")
                kind = body.get("kind")
                if not isinstance(kind, str) or kind not in {"pptx", "xlsx"}:
                    raise ValueError("请选择 PPTX 或 XLSX 文件。")
                if CONTROLLER.task_active:
                    raise ValueError("请等待当前任务结束后再选择文件。")
                if not DIRECTORY_PICKER_LOCK.acquire(blocking=False):
                    raise ValueError("文件选择窗口已打开。")
                try:
                    paths = (validate_selected_paths(body["paths"], kind, multiple=False)
                             if "paths" in body else choose_files(kind, multiple=False))
                finally:
                    DIRECTORY_PICKER_LOCK.release()
                self._send(200, {"fields": collect_labels(paths[0]), "filename": Path(paths[0]).name}
                           if paths else {"cancelled": True})
            elif self.path == "/api/local-xlsx":
                paths = validate_selected_paths(body.get("paths"), "xlsx", multiple=False)
                with open(paths[0], "rb") as stream:
                    content = stream.read(MAX_TEMPLATE_BYTES + 1)
                if not content or len(content) > MAX_TEMPLATE_BYTES:
                    raise ValueError("请选择 5 MB 以内的 XLSX 文件。")
                self._send(200, {"filename": Path(paths[0]).name, "content": base64.b64encode(content).decode("ascii")})
            elif self.path == "/api/template-builder/download":
                LICENSES.require_valid(feature="template_builder")
                content = create_template(body.get("fields"))
                self._send(200, {"filename": "面辅料提取模板.xlsx", "content": base64.b64encode(content).decode("ascii")})
            elif self.path == "/api/updates/rollback":
                with CONTROLLER.maintenance():
                    status = UPDATES.rollback()
                self._send(200, status)
            elif self.path == "/api/pptx/settings":
                if CONTROLLER.task_active:
                    raise ValueError("请等待任务完成后再更新提取配置。")
                with CONFIG_LOCK:
                    result = save_pptx_settings(data_dir(), body)
                self._send(200, result)
            elif self.path == "/api/pptx/files":
                if not DIRECTORY_PICKER_LOCK.acquire(blocking=False):
                    raise ValueError("文件选择窗口已打开。")
                try:
                    if "paths" in body:
                        paths = validate_selected_paths(body["paths"], body.get("kind"), multiple=body.get("multiple"))
                    elif "multiple" in body:
                        paths = choose_files(body.get("kind"), multiple=body["multiple"])
                    else:
                        paths = choose_files(body.get("kind"))
                finally:
                    DIRECTORY_PICKER_LOCK.release()
                self._send(200, {"paths": paths})
            elif self.path == "/api/pptx/template":
                if CONTROLLER.task_active:
                    raise ValueError("请等待当前任务完成后再分析模板。")
                template = validate_pptx_file(body.get("template"), "xlsx")
                result = invoke_pptx({"action": "analyze_template", "template": template,
                                      "rules": body.get("rules", [])}, CONTROLLER.shutdown_event,
                                     lambda event: None, timeout=60)
                self._send(200, result)
            elif self.path == "/api/pptx/run":
                CONTROLLER.start({"name": "PPTX 面辅料提取", "kind": "pptx_extract",
                                  "module_id": "pptx_material_extract", "request": body})
                self._send(200, {"ok": True})
            elif self.path == "/api/license/request":
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
            elif self.path == "/api/task/reset":
                self._send(200, {"task": CONTROLLER.reset()})
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

    def _install_update(self):
        from offline_updates import MAX_BYTES
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BYTES or self.headers.get("Transfer-Encoding"):
            raise ValueError("请选择 128 MB 以内的完整更新包。")
        temporary = None
        previous_timeout = self.connection.gettimeout()
        try:
            with CONTROLLER.maintenance():
                self.connection.settimeout(30)
                with tempfile.NamedTemporaryFile(prefix=".update-upload-", suffix=".zip",
                                                 dir=data_dir(), delete=False) as stream:
                    temporary = Path(stream.name)
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("更新包上传不完整，请重新选择。")
                        stream.write(chunk)
                        remaining -= len(chunk)
                status = UPDATES.install(temporary)
            self._send(200, status)
        finally:
            self.connection.settimeout(previous_timeout)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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
        from update_keys import BASE_RELEASE_SEQUENCE, UPDATE_PUBLIC_KEYS
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from playwright._impl._driver import compute_driver_executable
        from openpyxl import Workbook
        if not PUBLIC_KEYS:
            raise SystemExit("No embedded public keys configured.")
        for public in PUBLIC_KEYS.values():
            Ed25519PublicKey.from_public_bytes(public)
        if (set(UPDATE_PUBLIC_KEYS) & set(PUBLIC_KEYS)
                or type(BASE_RELEASE_SEQUENCE) is not int or BASE_RELEASE_SEQUENCE < 1):
            raise SystemExit("Update trust configuration is invalid.")
        for public in UPDATE_PUBLIC_KEYS.values():
            Ed25519PublicKey.from_public_bytes(public)
        node, driver = compute_driver_executable()
        if not Path(node).is_file() or not Path(driver).is_file():
            raise SystemExit("Bundled Playwright driver is missing.")
        workbook = Workbook()
        workbook.close()
        UpdateManager.healthcheck(Path(__file__).resolve().parent)
        print("AIOA compiled update resources and verification keys OK")
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
