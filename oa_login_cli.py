#!/usr/bin/env python3
"""Local OA automation workbench. Run with Python, then use its browser UI."""

import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List
from urllib.parse import urlparse


APP_NAME = "oa-login-cli"
MAX_OUTPUT_LINES = 120
MAX_BODY_BYTES = 32_768
CONFIG_LOCK = threading.Lock()


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


def save_operations(operations: List[dict]) -> None:
    target = data_dir() / "operations.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(operations[:50], ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def normalize_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("请输入有效的 http/https 地址。")
    return value


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


class Cancelled(Exception):
    pass


class AutomationController:
    """Owns the Playwright worker and exposes only non-secret state to HTTP."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.messages = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.state = {
            "phase": "idle",
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
            self.state.update(
                phase=phase,
                message=message,
                updated_at=datetime.now().strftime("%H:%M:%S"),
                **values,
            )

    def start(self, operation: dict) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("已有任务正在运行，请先等待或停止。")
            self.messages = queue.Queue()
            self.cancel_event.clear()
            self.state = {
                "phase": "opening",
                "message": "正在打开业务页面…",
                "operation": operation["name"],
                "title": "",
                "url": "",
                "content": "",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
            self.worker = threading.Thread(
                target=self._run, args=(dict(operation),), daemon=True
            )
            self.worker.start()

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
            if not self.worker or not self.worker.is_alive():
                raise ValueError("当前没有运行中的任务。")
            self.cancel_event.set()
        self.messages.put(("cancel",))
        self.update("stopping", "正在停止任务…")

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def _login_form_visible(self, page: Any) -> bool:
        try:
            return find_login_form(page)[0] is not None
        except Exception:
            return False

    def _wait_for_login(self, page: Any) -> None:
        submitted_at = None
        self.update(
            "login_required",
            "检测到登录页面。可在此输入账号密码，也可直接在已打开的 OA 浏览器中登录。",
        )
        while True:
            self.check_cancelled()
            try:
                message = self.messages.get(timeout=1)
            except queue.Empty:
                message = None

            if message and message[0] == "credentials":
                _, username, password = message
                try:
                    frame, password_input = fill_login_form(page, username, password)
                    click_login(frame, password_input)
                    self.update(
                        "authenticating",
                        "已提交登录信息；如需验证码或二次验证，请在 OA 浏览器中完成。",
                    )
                    submitted_at = time.monotonic()
                except Exception as error:
                    self.update("login_required", f"自动填写失败：{error}。可在 OA 浏览器中手动登录。")
                finally:
                    password = ""

            self.check_cancelled()
            if not self._login_form_visible(page):
                page.wait_for_timeout(1100)
                if not self._login_form_visible(page):
                    return
            elif submitted_at and time.monotonic() - submitted_at > 10:
                self.update(
                    "login_required",
                    "登录表单仍在页面上。请检查账号密码，或在 OA 浏览器中完成验证码和二次验证。",
                )
                submitted_at = None

    def _run(self, operation: dict) -> None:
        context = None
        final_phase = "error"
        final_message = "任务中断。"
        result = {}
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = launch_context(playwright)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(operation["url"], wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(900)
                self.check_cancelled()

                if self._login_form_visible(page):
                    self._wait_for_login(page)
                    self.update("opening", "登录完成，正在返回业务页面…")
                    page.goto(operation["url"], wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(900)
                    self.check_cancelled()
                    if self._login_form_visible(page):
                        raise RuntimeError("返回业务页面后仍显示登录表单，请检查账号或登录状态。")

                self.update("reading", "正在读取业务页面主要内容…")
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
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            self.update(final_phase, final_message, **result)


PAGE_HTML = r'''<!doctype html>
<html lang="zh-CN">
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
    @media(max-width:1060px){.shell{grid-template-columns:72px minmax(0,1fr)}.sidebar{padding:30px 11px}.brand{justify-content:center;padding:0 0 35px}.brand-copy,.nav-label,.nav-item span,.side-bottom{display:none}.nav-item{justify-content:center;padding:13px}.nav-item svg{width:20px;height:20px}}
    @media(max-width:800px){.workspace{grid-template-columns:1fr}.overview{grid-template-columns:repeat(3,minmax(0,1fr))}.content{padding:26px 20px 45px}.topbar{padding:0 20px}.hero{min-height:150px}}
    @media(max-width:560px){.shell{display:block}.sidebar{height:58px;position:static;flex-direction:row;align-items:center;padding:0 17px}.brand{padding:0}.brand-copy{display:block}.brand-sub{display:none}.nav-label,.nav-item,.side-bottom{display:none}.topbar{height:55px}.content{padding:24px 15px 40px}.page-title{font-size:25px}.hero{padding:24px}.hero h2{font-size:20px}.overview{gap:8px}.metric{padding:12px;gap:7px}.metric-icon{display:none}.metric-value{font-size:12px}.panel-heading,.run-body,.detail{padding-left:17px;padding-right:17px}.search-wrap{padding-left:17px;padding-right:17px}.search-wrap svg{left:30px}.login-grid{grid-template-columns:1fr}.top-pill{display:none}}
  </style>
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
          <div class="panel-heading"><div><p class="section-kicker">BUSINESS OPERATIONS</p><h3>业务操作</h3><div class="panel-sub">选择已配置业务，或创建一个新入口</div></div><button class="primary with-icon" id="addButton"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>新增业务</button></div>
          <div class="search-wrap"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/></svg><input class="search" id="searchInput" placeholder="搜索业务名称或地址" aria-label="搜索业务"></div>
          <div class="operation-list" id="operationList"></div>
          <div class="detail"><div class="detail-head"><span class="detail-label">当前选择</span><div class="detail-actions"><button class="text-btn" id="editButton" hidden>编辑</button><button class="text-btn danger" id="deleteButton" hidden>删除</button></div></div><div id="selectionDetail" class="detail-none">尚未选择业务操作</div></div>
        </section>
        <section class="panel" aria-label="运行任务">
          <div class="panel-heading run-head"><div><p class="section-kicker">TASK EXECUTION</p><h3>运行任务</h3><div class="panel-sub">执行过程和页面结果实时更新</div></div><span class="status-badge" id="statusBadge">待开始</span></div>
          <div class="run-body">
            <div class="timeline"><div class="step active" id="step1"><span class="step-num">1</span><span class="step-label">选择业务</span></div><div class="step-line"></div><div class="step" id="step2"><span class="step-num">2</span><span class="step-label">验证登录</span></div><div class="step-line"></div><div class="step" id="step3"><span class="step-num">3</span><span class="step-label">读取页面</span></div></div>
            <div class="status-card"><div class="status-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg></div><div><div class="status-title" id="statusTitle">准备就绪</div><div class="status-message" id="statusMessage">选择左侧业务操作后，即可开始。</div></div></div>
            <div class="login-card" id="loginCard" hidden><h4>完成 OA 登录</h4><p>可在这里填写账号密码；如果 OA 要求验证码、扫码或二次验证，请在自动打开的浏览器中完成。</p><form id="loginForm"><div class="login-grid"><div><label class="field-label" for="username">用户名</label><input class="field" id="username" autocomplete="username" required></div><div><label class="field-label" for="password">密码</label><input class="field" id="password" type="password" autocomplete="current-password" required></div></div><button class="primary login-submit" type="submit" id="loginButton">提交登录信息</button></form></div>
            <div class="run-actions"><button class="primary" id="runButton" disabled>开始运行</button><button class="ghost" id="stopButton" hidden>停止任务</button></div>
            <p class="fine-print">密码仅用于本次登录，不写入业务配置。浏览器会在读取页面后自动关闭。</p>
            <div class="result" id="result" hidden><div class="result-top"><h4>页面主要内容</h4><span class="result-meta" id="resultTime"></span></div><div class="result-title" id="resultTitle"></div><p class="result-url" id="resultUrl"></p><pre id="resultContent"></pre></div>
          </div>
        </section>
      </div>
      <div class="guide"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg><div><strong>当前版本说明</strong><p>已支持业务入口管理、登录协助和页面内容读取。单号查询、列表筛选及勾选操作将在后续业务规则明确后接入。</p></div></div>
    </main>
  </div>
</div>
<div class="modal-backdrop" id="modalBackdrop" role="presentation"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle"><div class="modal-top"><h3 id="modalTitle">新增业务操作</h3><button class="icon-btn" id="closeModal" aria-label="关闭">×</button></div><p>填写此业务的名称和页面地址，之后可直接选择使用。</p><form id="operationForm"><label class="field-label" for="operationName">业务名称</label><input class="field" id="operationName" maxlength="60" placeholder="例如：订单查询" required><label class="field-label" for="operationUrl">业务页面 URL</label><input class="field" id="operationUrl" type="url" placeholder="https://example.com/oa/orders" required><div class="modal-actions"><button class="ghost" type="button" id="cancelModal">取消</button><button class="primary" type="submit" id="saveButton">保存业务</button></div></form></div></div>
<div class="toast" id="toast" role="status"></div>
<script>
  const TOKEN = '__AIOA_TOKEN__';
  const $ = id => document.getElementById(id);
  let operations = [], selectedName = '', currentTask = {phase:'idle'}, editingName = '', toastTimer, initialPromptShown = false;
  const runningPhases = new Set(['opening','login_required','authenticating','reading','stopping']);
  const labels = {idle:'待开始',opening:'打开页面',login_required:'等待登录',authenticating:'验证中',reading:'读取中',stopping:'停止中',done:'已完成',error:'执行失败',cancelled:'已停止'};
  function toast(message){const node=$('toast');node.textContent=message;node.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.classList.remove('show'),3600)}
  async function api(path,method='GET',body){const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-AIOA-Token':TOKEN},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});let data;try{data=await response.json()}catch{data={error:'无法读取服务器响应。'}}if(!response.ok)throw new Error(data.error||'请求失败。');return data}
  function selected(){return operations.find(item=>item.name===selectedName)}
  function renderOperations(){
    const list=$('operationList');list.replaceChildren();const query=$('searchInput').value.trim().toLocaleLowerCase();const filtered=operations.filter(item=>(item.name+' '+item.url).toLocaleLowerCase().includes(query));
    if(!filtered.length){const wrap=document.createElement('div');wrap.className='empty';wrap.innerHTML='<div class="empty-icon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 10h8M8 14h6"/></svg></div><h4>'+(operations.length?'没有找到匹配的业务':'还没有业务操作')+'</h4><p>'+(operations.length?'试试其他名称或地址':'点击右上角「新增业务」开始配置')+'</p>';list.append(wrap)}
    for(const item of filtered){const button=document.createElement('button');button.className='operation'+(item.name===selectedName?' selected':'');button.type='button';button.setAttribute('aria-pressed',item.name===selectedName?'true':'false');const icon=document.createElement('span');icon.className='op-icon';icon.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 6.5h16v11H4z"/><path d="M8 10h8M8 13.5h5"/></svg>';const copy=document.createElement('span');copy.className='op-copy';const name=document.createElement('div');name.className='op-name';name.textContent=item.name;const url=document.createElement('div');url.className='op-url';url.textContent=item.url;copy.append(name,url);const arrow=document.createElement('span');arrow.className='op-arrow';arrow.textContent='›';button.append(icon,copy,arrow);button.addEventListener('click',()=>{selectedName=item.name;renderOperations();renderDetail();renderTask()});list.append(button)}
    $('countValue').textContent=operations.length+' 项';
  }
  function renderDetail(){const item=selected();$('editButton').hidden=!item;$('deleteButton').hidden=!item;const box=$('selectionDetail');box.replaceChildren();if(!item){box.className='detail-none';box.textContent='尚未选择业务操作';return}box.className='';const n=document.createElement('div');n.className='selected-name';n.textContent=item.name;const u=document.createElement('p');u.className='selected-url';u.textContent=item.url;box.append(n,u)}
  function renderTask(){
    const phase=currentTask.phase||'idle', active=runningPhases.has(phase), badge=$('statusBadge');badge.className='status-badge '+(phase==='done'?'done':phase==='error'?'error':phase==='login_required'?'login_required':active?'running':'');badge.textContent=labels[phase]||phase;$('taskValue').textContent=labels[phase]||'待开始';
    $('statusTitle').textContent=phase==='idle'?'准备就绪':(currentTask.operation||'业务操作')+' · '+(labels[phase]||phase);$('statusMessage').textContent=phase==='idle'?'选择左侧业务操作后，即可开始。':currentTask.message||'';
    $('runButton').disabled=!selected()||active;$('runButton').textContent=phase==='done'||phase==='error'||phase==='cancelled'?'重新运行':'开始运行';$('stopButton').hidden=!active;$('stopButton').disabled=phase==='stopping';$('loginCard').hidden=phase!=='login_required';
    let step=phase==='idle'?1:phase==='opening'?1:['login_required','authenticating'].includes(phase)?2:3;for(let i=1;i<=3;i++){const node=$('step'+i);node.className='step'+(i<step?' complete':i===step?' active':'')}
    const hasResult=phase==='done'&&currentTask.content;$('result').hidden=!hasResult;if(hasResult){$('resultTitle').textContent=currentTask.title||'OA 业务页面';$('resultUrl').textContent=currentTask.url||'';$('resultContent').textContent=currentTask.content;$('resultTime').textContent=currentTask.updated_at||''}
  }
  async function refresh(){try{const data=await api('/api/state');operations=data.operations||[];currentTask=data.task||{phase:'idle'};if(selectedName&&!selected())selectedName='';if(!selectedName&&operations.length)selectedName=operations[0].name;renderOperations();renderDetail();renderTask();if(!initialPromptShown){initialPromptShown=true;if(!operations.length)openModal()}}catch(error){$('statusMessage').textContent='本地服务连接失败：'+error.message}}
  function openModal(edit=false){const item=edit?selected():null;editingName=item?item.name:'';$('modalTitle').textContent=item?'编辑业务操作':'新增业务操作';$('operationName').value=item?item.name:'';$('operationUrl').value=item?item.url:'';$('modalBackdrop').classList.add('open');$('operationName').focus()}
  function closeModal(){$('modalBackdrop').classList.remove('open');editingName=''}
  $('addButton').addEventListener('click',()=>openModal());$('editButton').addEventListener('click',()=>openModal(true));$('closeModal').addEventListener('click',closeModal);$('cancelModal').addEventListener('click',closeModal);$('modalBackdrop').addEventListener('click',event=>{if(event.target===$('modalBackdrop'))closeModal()});document.addEventListener('keydown',event=>{if(event.key==='Escape')closeModal()});$('searchInput').addEventListener('input',renderOperations);
  $('operationForm').addEventListener('submit',async event=>{event.preventDefault();const name=$('operationName').value.trim(),url=$('operationUrl').value.trim();try{await api('/api/operations','POST',{name,url,old_name:editingName});selectedName=name;closeModal();await refresh();toast('业务操作已保存')}catch(error){toast(error.message)}});
  $('deleteButton').addEventListener('click',async()=>{const item=selected();if(!item||!confirm('确定删除“'+item.name+'”吗？'))return;try{await api('/api/operations','DELETE',{name:item.name});selectedName='';await refresh();toast('业务操作已删除')}catch(error){toast(error.message)}});
  $('runButton').addEventListener('click',async()=>{const item=selected();if(!item)return;try{await api('/api/run','POST',{name:item.name});await refresh()}catch(error){toast(error.message)}});
  $('stopButton').addEventListener('click',async()=>{try{await api('/api/stop','POST',{});await refresh()}catch(error){toast(error.message)}});
  $('loginForm').addEventListener('submit',async event=>{event.preventDefault();const username=$('username').value,password=$('password').value;try{await api('/api/credentials','POST',{username,password});$('password').value='';await refresh();toast('登录信息已提交')}catch(error){toast(error.message)}});
  refresh();setInterval(refresh,1300);
</script>
</body>
</html>'''


CONTROLLER = AutomationController()
ACCESS_TOKEN = secrets.token_urlsafe(32)


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
        if length < 1 or length > MAX_BODY_BYTES:
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
                operations = load_operations()
            self._send(200, {"operations": operations, "task": CONTROLLER.snapshot()})
        else:
            self._send(404, {"error": "页面不存在。"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(403, {"error": "访问被拒绝。"})
            return
        try:
            body = self._body()
            if self.path == "/api/operations":
                name = str(body.get("name", "")).strip()
                old_name = str(body.get("old_name", "")).strip()
                if not name or len(name) > 60:
                    raise ValueError("业务名称须为 1–60 个字符。")
                url = normalize_url(str(body.get("url", "")))
                operation = {"name": name, "url": url}
                with CONFIG_LOCK:
                    existing = load_operations()
                    if old_name and not any(item["name"] == old_name for item in existing):
                        raise ValueError("原业务操作不存在，请刷新页面。")
                    if old_name and old_name.casefold() != name.casefold() and any(item["name"].casefold() == name.casefold() for item in existing):
                        raise ValueError("该业务名称已存在。")
                    if not old_name and any(item["name"].casefold() == name.casefold() for item in existing):
                        raise ValueError("该业务名称已存在，请使用编辑操作。")
                    if not old_name and len(existing) >= 50:
                        raise ValueError("最多保存 50 个业务操作。")
                    removed_name = old_name or name
                    save_operations([operation] + [item for item in existing if item["name"].casefold() != removed_name.casefold()])
                self._send(200, {"ok": True})
            elif self.path == "/api/run":
                name = str(body.get("name", ""))
                with CONFIG_LOCK:
                    operation = next((item for item in load_operations() if item["name"] == name), None)
                if operation is None:
                    raise ValueError("业务操作不存在，请刷新页面。")
                CONTROLLER.start(operation)
                self._send(200, {"ok": True})
            elif self.path == "/api/credentials":
                CONTROLLER.submit_credentials(str(body.get("username", "")), str(body.get("password", "")))
                self._send(200, {"ok": True})
            elif self.path == "/api/stop":
                CONTROLLER.stop()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "接口不存在。"})
        except (ValueError, json.JSONDecodeError) as error:
            self._send(400, {"error": str(error)})
        except Exception as error:
            self._send(500, {"error": f"服务器错误：{error}"})

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._send(403, {"error": "访问被拒绝。"})
            return
        if self.path != "/api/operations":
            self._send(404, {"error": "接口不存在。"})
            return
        try:
            name = str(self._body().get("name", ""))
            with CONFIG_LOCK:
                existing = load_operations()
                updated = [item for item in existing if item["name"] != name]
                if len(updated) == len(existing):
                    raise ValueError("业务操作不存在。")
                save_operations(updated)
            self._send(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as error:
            self._send(400, {"error": str(error)})


def run() -> int:
    configure_console()
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"AIOA 工作台已启动：{address}")
    print("关闭此窗口可退出；也可在这里按 Ctrl+C。")
    threading.Timer(0.8, lambda: webbrowser.open_new_tab(address)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        CONTROLLER.cancel_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
