"""Owner-only license issuer. Never include this module in customer payloads."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import secrets
import threading
import webbrowser
import zipfile

from offline_license import (PRODUCT, LicenseError, canonical_payload,
                             fetch_network_time, verify_license, _timestamp)


def create_keys(password: str) -> tuple[bytes, dict]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    import hashlib

    if len(password) < 8:
        raise LicenseError("私钥保护密码至少需要 8 个字符。")
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    key_id = hashlib.sha256(public).hexdigest()[:16]
    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(password.encode("utf-8")))
    return encrypted, {key_id: base64.b64encode(public).decode("ascii")}


def issue_license(request: dict, private_pem: bytes, password: str, customer: str,
                  days: float | None = None, expires_at: str | None = None,
                  now: float | None = None) -> bytes:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import hashlib
    import math

    if (not isinstance(request, dict) or type(request.get("version")) is not int
            or request["version"] != 1 or request.get("product") != PRODUCT
            or not re.fullmatch(r"AIOA-[0-9A-F]{64}", str(request.get("device_id", "")))
            or not re.fullmatch(r"[0-9a-f]{32}", str(request.get("request_id", "")))):
        raise LicenseError("设备申请文件格式不正确。")
    if (days is None) == (expires_at is None):
        raise LicenseError("请选择授权天数或明确到期时间其中一种。")
    if not customer.strip() or len(customer) > 200:
        raise LicenseError("请填写客户名称（最多 200 字）。")
    try:
        key = load_pem_private_key(private_pem, password=password.encode("utf-8"))
    except (ValueError, TypeError) as error:
        raise LicenseError("私钥文件或保护密码不正确。") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenseError("请选择本工具生成的 Ed25519 私钥。")
    public = key.public_key().public_bytes_raw()
    issued = datetime.fromtimestamp(fetch_network_time() if now is None else now, timezone.utc)
    if days is not None:
        if not math.isfinite(days) or not 0 < days <= 36500:
            raise LicenseError("授权天数必须大于 0，且不超过 36500 天。")
        end = issued + timedelta(days=days)
    else:
        end = datetime.fromtimestamp(_timestamp(expires_at), timezone.utc)
    if end <= issued:
        raise LicenseError("到期时间必须晚于当前联网时间。")
    payload = {"version": 1, "product": PRODUCT, "device_id": request["device_id"],
               "request_id": request["request_id"], "license_id": secrets.token_hex(16),
               "key_id": hashlib.sha256(public).hexdigest()[:16], "customer": customer.strip(),
               "features": ["process_sheet"], "issued_at": issued.isoformat(),
               "not_before": issued.isoformat(), "expires_at": end.isoformat()}
    document = json.dumps({"payload": payload, "signature": base64.b64encode(
        key.sign(canonical_payload(payload))).decode("ascii")}, ensure_ascii=False, indent=2).encode("utf-8")
    verify_license(document, {payload["key_id"]: public}, request["device_id"])
    return document


ISSUER_HTML = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AIOA 授权签发台</title>
<style> *{box-sizing:border-box}body{background:#171717;color:#ececec;font:15px/1.6 system-ui;margin:0}main{max-width:850px;margin:40px auto;padding:24px}h1{font-size:28px;margin:0}p,small{color:#aaa}section{background:#212121;border:1px solid #3b3b3b;border-radius:16px;padding:24px;margin:20px 0}h2{font-size:18px;margin:0 0 14px}label{display:block;font-size:13px;margin:12px 0 5px}input,select,button{font:inherit;border-radius:9px;border:1px solid #4b4b4b;padding:10px;background:#2a2a2a;color:#eee;width:100%}button{background:#ececec;color:#171717;margin-top:18px;cursor:pointer}button:disabled{opacity:.45}.row{display:grid;grid-template-columns:1fr 1fr;gap:18px}#message{white-space:pre-wrap;color:#8ee0ba}@media(max-width:600px){.row{grid-template-columns:1fr}}</style>
<main><h1>AIOA 授权签发台</h1><p>管理员专用 · 密钥与客户工作台分开保管</p>
<section><h2>首次创建签发密钥</h2><small>仅首次使用时创建。已有密钥请直接签发，重新创建的密钥无法用于旧工作台。</small><label>私钥保护密码（至少 8 位）</label><input id="newPassword" type="password" autocomplete="new-password"><label>再次输入密码</label><input id="confirmPassword" type="password" autocomplete="new-password"><button id="keygen">创建并下载密钥备份</button></section>
<section><h2>签发客户授权</h2><div class="row"><div><label>客户设备申请文件</label><input id="request" type="file" accept=".json"></div><div><label>签发私钥（private-key.pem）</label><input id="key" type="file" accept=".pem"></div></div><label>私钥保护密码</label><input id="password" type="password" autocomplete="off"><label>客户名称</label><input id="customer" maxlength="200"><div class="row"><div><label>有效期方式</label><select id="mode"><option value="days">按签发后的天数</option><option value="expires_at">指定到期时间</option></select></div><div><label id="durationLabel">授权天数</label><input id="days" type="number" min="0.01" step="0.01" value="30"><input id="expires" type="datetime-local" hidden></div></div><button id="issue">签发并下载授权文件</button><small>签发时会联网校时；客户导入后生效，起始时间为签发时间。</small></section><p id="message" role="status"></p></main>
<script>const token='__TOKEN__',$=id=>document.getElementById(id);async function api(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-AIOA-Token':token},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error);return data}function download(name,content,type){const url=URL.createObjectURL(new Blob([content],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000)}async function read(id){const file=$(id).files[0];if(!file)throw Error('请选择设备申请和私钥文件');if(file.size>65536)throw Error('文件过大');return file.text()}async function action(id,fn){$(id).disabled=true;$('message').textContent='处理中…';try{await fn()}catch(error){$('message').textContent=error.message}finally{$(id).disabled=false}}
$('mode').onchange=()=>{$('days').hidden=$('mode').value!=='days';$('expires').hidden=$('mode').value==='days';$('durationLabel').textContent=$('mode').value==='days'?'授权天数':'到期时间（本机时区）'};
$('keygen').onclick=()=>action('keygen',async()=>{if($('newPassword').value!==$('confirmPassword').value)throw Error('两次密码不一致');const data=await api('/api/keygen',{password:$('newPassword').value});download('AIOA-签发密钥备份.zip',Uint8Array.from(atob(data.archive),c=>c.charCodeAt(0)),'application/zip');$('newPassword').value='';$('confirmPassword').value='';$('message').textContent='密钥已生成。请备份 private-key.pem；仅把 public-keys.json 交给工作台构建工具。'});
$('issue').onclick=()=>action('issue',async()=>{const body={request:JSON.parse(await read('request')),private_key:await read('key'),password:$('password').value,customer:$('customer').value};if($('mode').value==='days')body.days=Number($('days').value);else{if(!$('expires').value)throw Error('请选择到期时间');body.expires_at=new Date($('expires').value).toISOString()}const data=await api('/api/issue',body);download(data.filename,data.document,'application/json');$('password').value='';$('key').value='';$('message').textContent='授权文件已签发，请交给对应客户导入工作台。'});
</script></html>'''


def serve(port=54420):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, content, kind="application/json"):
            data = (json.dumps(content, ensure_ascii=False) if kind == "application/json" else content).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def trusted(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.trusted() or self.path != "/":
                self.send(404, {"error": "页面不存在"})
            else:
                self.send(200, ISSUER_HTML.replace("__TOKEN__", token), "text/html")

        def do_POST(self):
            if (not self.trusted() or self.headers.get("X-AIOA-Token") != token
                    or self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}")):
                self.send(403, {"error": "访问被拒绝"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 192 * 1024:
                    raise ValueError("请求内容大小无效")
                body = json.loads(self.rfile.read(size))
                if self.path == "/api/keygen":
                    private, public = create_keys(str(body.get("password", "")))
                    stream = io.BytesIO()
                    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr("private-key.pem", private)
                        archive.writestr("public-keys.json", json.dumps(public, indent=2))
                        archive.writestr("保管说明.txt", "私钥只能由管理员保管，不得交给客户或放入代码仓库。请备份私钥和密码。public-keys.json 用于编译客户工作台。")
                    self.send(200, {"archive": base64.b64encode(stream.getvalue()).decode("ascii")})
                elif self.path == "/api/issue":
                    content = issue_license(body["request"], body["private_key"].encode(),
                                            str(body.get("password", "")), str(body.get("customer", "")),
                                            days=body.get("days"), expires_at=body.get("expires_at"))
                    number = json.loads(content)["payload"]["license_id"]
                    self.send(200, {"document": content.decode("utf-8"), "filename": f"AIOA-{number}.license"})
                else:
                    self.send(404, {"error": "接口不存在"})
            except (ValueError, KeyError, TypeError, OverflowError) as error:
                self.send(400, {"error": str(error)})
            except Exception:
                self.send(500, {"error": "签发失败，请检查文件和网络后重试。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"授权签发台：http://127.0.0.1:{server.server_port}/\n关闭此窗口可退出。", flush=True)
    threading.Timer(.8, lambda: webbrowser.open_new_tab(f"http://127.0.0.1:{server.server_port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="AIOA 管理员授权签发工具")
    commands = parser.add_subparsers(dest="command")
    web = commands.add_parser("serve")
    web.add_argument("--port", type=int, default=54420)
    init = commands.add_parser("init")
    init.add_argument("--output", type=Path, required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--request", type=Path, required=True)
    issue.add_argument("--key", type=Path, required=True)
    issue.add_argument("--customer", required=True)
    duration = issue.add_mutually_exclusive_group(required=True)
    duration.add_argument("--days", type=float)
    duration.add_argument("--expires-at")
    issue.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command in (None, "serve"):
            serve(getattr(args, "port", 54420))
        elif args.command == "init":
            password = getpass.getpass("私钥保护密码：")
            if password != getpass.getpass("再次输入密码："):
                raise LicenseError("两次密码不一致。")
            private, public = create_keys(password)
            args.output.mkdir(parents=True, exist_ok=False)
            target = args.output / "private-key.pem"
            with target.open("xb") as stream:
                stream.write(private)
            target.chmod(0o600)
            (args.output / "public-keys.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
            print("密钥已生成，请单独备份私钥和保护密码。")
        elif args.command == "issue":
            content = issue_license(json.loads(args.request.read_bytes()), args.key.read_bytes(),
                                    getpass.getpass("私钥保护密码："), args.customer,
                                    days=args.days, expires_at=args.expires_at)
            with args.output.open("xb") as stream:
                stream.write(content)
            print(f"授权已保存：{args.output}")
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
