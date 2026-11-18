"""Owner-only encrypted release key creation and offline update signing."""
import argparse
import base64
from datetime import datetime, timezone
import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import webbrowser
from zipfile import ZipFile, ZIP_DEFLATED

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offline_updates import (BASE_API, MAX_BYTES, PRODUCT, WEB_FILES, UpdateError,
                             UpdateStore, _atomic_json, canonical, verify_manifest)
from update_manager import UpdateManager

ROOT = Path(__file__).resolve().parent


def public_export(key):
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16], raw


def create_keys(password):
    if not isinstance(password, str) or len(password) < 8:
        raise UpdateError("请设置至少 8 位的私钥保护密码。")
    key = Ed25519PrivateKey.generate()
    identifier, raw = public_export(key)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password.encode("utf-8")))
    return private, {identifier: base64.b64encode(raw).decode("ascii")}


def load_key(private, password):
    if not isinstance(private, bytes) or len(private) > 16384 or not isinstance(password, str) or not password:
        raise UpdateError("请选择加密更新私钥并填写保护密码。")
    try:
        key = serialization.load_pem_private_key(private, password=password.encode("utf-8"))
    except (ValueError, TypeError, UnsupportedAlgorithm) as error:
        raise UpdateError("更新私钥无法解密，请检查文件和密码。") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise UpdateError("更新私钥必须是 Ed25519 类型。")
    return key


def make_package(web_root, version, sequence, key, component=None):
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", version):
        raise UpdateError("版本名称仅支持 1 至 64 位英文字母、数字、点、下划线和短横线。")
    if type(sequence) is not int or sequence < 1:
        raise UpdateError("版本序号必须是正整数。")
    files = {}
    for name in sorted(WEB_FILES):
        path = Path(web_root) / Path(name).name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
            raise UpdateError(f"界面文件缺失、过大或为链接：{path.name}")
        with path.open("rb") as source:
            files[name] = source.read(MAX_BYTES + 1)
    # Check a detached snapshot, so concurrent source edits cannot change signed content.
    with tempfile.TemporaryDirectory(prefix="oaf-release-check-") as temporary:
        root = Path(temporary)
        (root / "web").mkdir()
        for name, data in files.items():
            (root / name).write_bytes(data)
        UpdateManager.healthcheck(root)
    if component:
        path = Path(component)
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
            raise UpdateError("请选择有效的 Windows x64 PPTX 提取组件。")
        with path.open("rb") as source:
            binary = source.read(MAX_BYTES + 1)
        offset = int.from_bytes(binary[60:64], "little") if len(binary) >= 64 else -1
        if (not binary.startswith(b"MZ") or offset < 64
                or binary[offset:offset + 6] != b"PE\x00\x00\x64\x86"):
            raise UpdateError("提取组件不是 Windows x64 可执行文件。")
        files["pptx_extractor/bin/pptx-extractor.exe"] = binary
    if sum(map(len, files.values())) > MAX_BYTES:
        raise UpdateError("更新内容超过 128 MB。")
    identifier, public = public_export(key)
    manifest = {"format": 1, "product": PRODUCT, "base_api": BASE_API,
                "version": version, "sequence": sequence,
                "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in files.items()}}
    envelope = {"manifest": manifest, "key_id": identifier,
                "signature": base64.b64encode(key.sign(canonical(manifest))).decode("ascii")}
    raw = canonical(envelope)
    verify_manifest(raw, {identifier: public})
    result = io.BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("update.json", raw)
        for name, data in files.items():
            archive.writestr("payload/" + name, data)
    package = result.getvalue()
    if len(package) > MAX_BYTES:
        raise UpdateError("更新包超过 128 MB。")
    return package


def make_unsigned_package(web_root, version, sequence, component=None):
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", version):
        raise UpdateError("版本名称仅支持 1 至 64 位英文字母、数字、点、下划线和短横线。")
    if type(sequence) is not int or sequence < 1:
        raise UpdateError("版本序号必须是正整数。")
    files = {}
    for name in sorted(WEB_FILES):
        path = Path(web_root) / Path(name).name
        if path.is_symlink() or not path.is_file():
            raise UpdateError(f"界面文件缺失或无效：{path.name}")
        files[name] = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="oaf-update-check-") as temporary:
        root = Path(temporary)
        (root / "web").mkdir()
        for name, data in files.items():
            (root / name).write_bytes(data)
        UpdateManager.healthcheck(root)
    if component:
        binary = Path(component).read_bytes()
        offset = int.from_bytes(binary[60:64], "little") if len(binary) >= 64 else -1
        if not binary.startswith(b"MZ") or offset < 64 or binary[offset:offset + 6] != b"PE\x00\x00\x64\x86":
            raise UpdateError("提取组件不是 Windows x64 可执行文件。")
        files["pptx_extractor/bin/pptx-extractor.exe"] = binary
    manifest = {"format": 1, "product": PRODUCT, "base_api": BASE_API,
                "version": version, "sequence": sequence,
                "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in files.items()}}
    envelope = {"manifest": manifest, "unsigned": True}
    result = io.BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("update.json", canonical(envelope))
        for name, data in files.items():
            archive.writestr("payload/" + name, data)
    return result.getvalue()


class Publisher:
    def __init__(self, directory):
        self.directory = Path(directory)

    def issue(self, web_root, version, private, password, sequence=None, component=None):
        key = load_key(private, password)
        identifier, _ = public_export(key)
        store = UpdateStore(self.directory, {})
        with store._lock():
            path = self.directory / "release-ledger.json"
            ledger = {}
            if path.exists():
                if path.stat().st_size > 65536:
                    raise UpdateError("发布记录损坏，请恢复发布记录备份。")
                try:
                    ledger = json.loads(path.read_bytes())
                except (ValueError, UnicodeError) as error:
                    raise UpdateError("发布记录损坏，请恢复发布记录备份。") from error
                if (not isinstance(ledger, dict) or any(
                        not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{16}", name)
                        or type(value) is not int or value < 1 for name, value in ledger.items())):
                    raise UpdateError("发布记录损坏，请恢复发布记录备份。")
            previous = ledger.get(identifier, 0)
            if sequence is None:
                timestamp = int(datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
                sequence = max(timestamp, previous + 1)
            if type(sequence) is not int or sequence <= previous:
                raise UpdateError("发布序号必须大于该密钥的上一次发布序号。")
            package = make_package(web_root, version, sequence, key, component)
            ledger[identifier] = sequence
            _atomic_json(path, ledger)
            return package


PUBLISHER_HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>软件更新签发</title>
<style>:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color-scheme:dark;background:#171717;color:#eee}*{box-sizing:border-box}body{max-width:780px;margin:36px auto;padding:0 20px}h1{font-size:25px}p,small{color:#aaa;line-height:1.7}section{background:#232323;padding:24px;border:1px solid #3b3b3b;border-radius:16px;margin:20px 0}h2{font-size:18px;margin-top:0}label{display:block;font-size:13px;margin:18px 0 8px}input,button{font:inherit;border-radius:9px;padding:11px 13px}input{width:100%;background:#191919;border:1px solid #555;color:#eee}button{border:0;background:#eee;color:#171717;margin-top:18px;cursor:pointer}button:disabled{opacity:.45;cursor:wait}#message{white-space:pre-wrap;color:#c4e3d4}code{overflow-wrap:anywhere}</style>
<h1>软件更新签发</h1><p>管理员工具。更新发布密钥与软件许可证密钥分别保管；客户只接收更新包。</p>
<section><h2>首次创建更新密钥</h2><small>已有更新密钥请直接签发。重新创建密钥后，客户基础安装包也需要嵌入新公钥。</small>
<label for="newPassword">私钥保护密码（至少 8 位）</label><input id="newPassword" type="password" autocomplete="new-password">
<label for="confirmPassword">再次输入密码</label><input id="confirmPassword" type="password" autocomplete="new-password">
<button id="createKeys">创建并下载更新密钥备份</button></section>
<section><h2>签发更新包</h2><label for="webRoot">界面资源目录</label><input id="webRoot" value="__WEB_ROOT__">
<label for="version">版本名称</label><input id="version" placeholder="例如 0.3.1">
<label for="sequence">发布序号（可留空，自动递增）</label><input id="sequence" type="number" min="1" step="1">
<label for="privateKey">加密更新私钥 update-private-key.pem</label><input id="privateKey" type="file" accept=".pem">
<label for="password">私钥保护密码</label><input id="password" type="password" autocomplete="off">
<label for="component">Windows PPTX 组件完整路径（可选）</label><input id="component" placeholder="留空时只更新界面，提取组件使用基础安装版">
<button id="build">签发并下载更新包</button><p>发布前自动检查签名、文件完整性和界面脚本语法。OA 后端及运行环境更新仍需完整安装包。</p></section>
<p id="message" role="status"></p><script>
const TOKEN='__TOKEN__',$=id=>document.getElementById(id);let busy=false;
async function action(path,body,filename){if(busy)return;busy=true;for(const button of document.querySelectorAll('button'))button.disabled=true;$('message').textContent='处理中，请稍候…';try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Update-Token':TOKEN},body:JSON.stringify(body)});if(!response.ok){const result=await response.json();throw Error(result.error||'操作失败')}const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);$('message').textContent=path==='/api/keygen'?'已下载更新密钥备份。请妥善保管私钥和密码，将解压后的 update-public-keys.json 用于基础安装包。':'更新包已下载，请先在测试工作台验证，再交付客户。'}catch(error){$('message').textContent=error.message}finally{for(const id of ['newPassword','confirmPassword','password','privateKey'])$(id).value='';busy=false;for(const button of document.querySelectorAll('button'))button.disabled=false}}
$('createKeys').onclick=()=>{if($('newPassword').value!==$('confirmPassword').value){$('message').textContent='两次密码不一致';return}action('/api/keygen',{password:$('newPassword').value},'Office-Auto-Flow-更新密钥备份.zip')};
$('build').onclick=async()=>{if(busy)return;const file=$('privateKey').files[0];if(!file||file.size>16384){$('message').textContent='请选择加密更新私钥文件';return}const version=$('version').value.trim();if(!/^[A-Za-z0-9_.-]{1,64}$/.test(version)){$('message').textContent='请填写有效版本名称';return}const sequence=$('sequence').value.trim();if(sequence&&(!Number.isSafeInteger(Number(sequence))||Number(sequence)<1)){$('message').textContent='发布序号必须是正整数';return}action('/api/build',{web_root:$('webRoot').value.trim(),version,sequence:sequence?Number(sequence):null,private_key:await file.text(),password:$('password').value,component:$('component').value.trim()},'Office-Auto-Flow-'+version+'.oaf-update')};
</script></html>'''


def serve(publisher, port, open_browser=True):
    import html
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def trusted(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def send(self, status, content, kind):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if not self.trusted() or self.path != "/":
                self.send(404, b"Not found", "text/plain; charset=utf-8")
                return
            page = PUBLISHER_HTML.replace("__WEB_ROOT__", html.escape(str(ROOT / "web"), quote=True)).replace("__TOKEN__", token)
            self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.trusted() or self.headers.get("Origin") not in (None, origin)
                    or self.headers.get("X-Update-Token") != token):
                self.send(403, b'{"error":"Access denied"}', "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise UpdateError("请求内容大小无效。")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise UpdateError("请求内容无效。")
                if self.path == "/api/keygen":
                    private, public = create_keys(body.get("password"))
                    content = io.BytesIO()
                    with ZipFile(content, "w", ZIP_DEFLATED) as archive:
                        archive.writestr("update-private-key.pem", private)
                        archive.writestr("update-public-keys.json", json.dumps(public, indent=2))
                    result = content.getvalue()
                elif self.path == "/api/build":
                    private = body.get("private_key")
                    if not isinstance(private, str):
                        raise UpdateError("请选择加密更新私钥。")
                    web_root = body.get("web_root")
                    component = body.get("component")
                    if not isinstance(web_root, str) or not isinstance(component, str):
                        raise UpdateError("资源或组件路径无效。")
                    result = publisher.issue(web_root, body.get("version"), private.encode("utf-8"),
                                             body.get("password"), body.get("sequence"), component or None)
                else:
                    self.send(404, b'{"error":"Not found"}', "application/json")
                    return
                self.send(200, result, "application/zip")
            except (ValueError, OSError, TypeError) as error:
                self.send(400, json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8"), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"更新签发工具：{address}", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open_new_tab(address)).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    keygen = subparsers.add_parser("keygen")
    keygen.add_argument("--output", type=Path, required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--web-root", type=Path, default=ROOT / "web")
    build.add_argument("--private-key", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--sequence", type=int)
    build.add_argument("--component", type=Path)
    unsigned = subparsers.add_parser("build-unsigned")
    unsigned.add_argument("--web-root", type=Path, default=ROOT / "web")
    unsigned.add_argument("--output", type=Path, required=True)
    unsigned.add_argument("--version", required=True)
    unsigned.add_argument("--sequence", type=int, required=True)
    unsigned.add_argument("--component", type=Path)
    for command in (build, subparsers.add_parser("serve")):
        command.add_argument("--state-dir", type=Path, default=ROOT / ".secrets/update-publisher")
    server_parser = subparsers.choices["serve"]
    server_parser.add_argument("--port", type=int, default=54423)
    server_parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "keygen":
            password = getpass.getpass("更新私钥保护密码：")
            if password != getpass.getpass("再次输入密码："):
                raise UpdateError("两次密码不一致。")
            private, public = create_keys(password)
            args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
            try:
                path = args.output / "update-private-key.pem"
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    stream.write(private)
                (args.output / "update-public-keys.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
            except BaseException:
                shutil.rmtree(args.output, ignore_errors=True)
                raise
            print(f"更新密钥已创建：{args.output}")
        elif args.command == "build":
            if args.output.exists():
                raise UpdateError("输出文件已存在，请指定新的文件名。")
            package = Publisher(args.state_dir).issue(args.web_root, args.version, args.private_key.read_bytes(),
                                                       getpass.getpass("更新私钥保护密码："), args.sequence, args.component)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            created = False
            try:
                with args.output.open("xb") as stream:
                    created = True
                    stream.write(package)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                if created:
                    args.output.unlink(missing_ok=True)
                raise
            print(f"更新包：{args.output}\n大小：{len(package)} bytes\nSHA-256：{hashlib.sha256(package).hexdigest()}")
        elif args.command == "build-unsigned":
            if args.output.exists():
                raise UpdateError("输出文件已存在，请指定新的文件名。")
            package = make_unsigned_package(args.web_root, args.version, args.sequence, args.component)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("xb") as stream:
                stream.write(package)
            print(f"更新包：{args.output}\n大小：{len(package)} bytes")
        else:
            server = serve(Publisher(args.state_dir), args.port, not args.no_browser)
            try:
                server.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
