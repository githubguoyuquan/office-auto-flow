"""Compile a native standalone workbench or separate owner-only issuer."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
APP_MODULES = ("oa_login_cli.py", "trim_template.py", "offline_license.py", "license_manager.py", "customer_profile.py", "material_suppliers.py", "pptx_service.py", "ui_resources.py")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


def load_public_keys(path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value:
        raise ValueError("请选择签发工具导出的 public-keys.json，不能使用空公钥集。")
    keys = {}
    for name, encoded in value.items():
        if not isinstance(name, str) or len(name) > 64:
            raise ValueError("公钥标识无效。")
        raw = base64.b64decode(encoded, validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
        if name != hashlib.sha256(raw).hexdigest()[:16]:
            raise ValueError("公钥标识与公钥内容不一致。")
        keys[name] = raw
    return keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("workbench", "issuer"), default="workbench")
    parser.add_argument("--public-keys", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.exists():
        parser.error("输出目录已存在，请指定新目录，避免覆盖已有交付。")
    if args.target == "workbench" and sys.platform != "win32":
        parser.error("客户工作台必须在 Windows x64 上编译；请运行 Windows 一键构建或 GitHub Actions。")
    keys = load_public_keys(args.public_keys) if args.target == "workbench" and args.public_keys else {}
    if args.target == "workbench" and not keys:
        parser.error("必须指定 --public-keys public-keys.json。")
    if args.jobs < 1:
        parser.error("--jobs 必须大于 0。")
    with tempfile.TemporaryDirectory(prefix="aioa-compile-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        modules = APP_MODULES if args.target == "workbench" else ("license_issuer.py", "offline_license.py")
        for filename in modules:
            shutil.copy2(ROOT / filename, source / filename)
        if args.target == "workbench":
            (source / "license_keys.py").write_text("PUBLIC_KEYS = " + repr(keys) + "\n", encoding="utf-8")
        entry = "oa_login_cli" if args.target == "workbench" else "license_issuer"
        name = "AIOA" if args.target == "workbench" else "AIOA-Issuer"
        executable = name + (".exe" if sys.platform == "win32" else "")
        command = [sys.executable, "-m", "nuitka", "--mode=standalone", "--python-flag=isolated",
                   "--assume-yes-for-downloads", f"--jobs={args.jobs}", "--lto=yes",
                   f"--output-dir={root / 'build'}", f"--output-filename={executable}",
                   "--include-package=cryptography", "--include-package-data=certifi",
                   "--nofollow-import-to=license_issuer" if args.target == "workbench" else "--nofollow-import-to=oa_login_cli"]
        if sys.platform == "win32":
            command.extend(["--msvc=latest", "--windows-console-mode=force"])
        if args.target == "workbench":
            profile_path = ROOT / "customer-profile.json"
            if profile_path.is_file():
                shutil.copy2(profile_path, source / profile_path.name)
            command.extend(["--include-package=playwright", "--include-package-data=playwright",
                            "--include-package=openpyxl"])
            if profile_path.is_file():
                command.append(f"--include-data-files={source / profile_path.name}=customer-profile.json")
        command.append(str(source / (entry + ".py")))
        subprocess.run(command, cwd=source, check=True)
        dist = root / "build" / (entry + ".dist")
        if not (dist / executable).is_file():
            raise RuntimeError("编译未生成预期的程序。")
        if args.target == "workbench":
            shutil.copytree(ROOT / "web", dist / "web")
            go_root = ROOT / "pptx_extractor"
            environment = dict(os.environ, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0")
            subprocess.run(["go", "test", "./..."], cwd=go_root, check=True)
            component = dist / "pptx_extractor" / "bin" / "pptx-extractor.exe"
            component.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(component), "."],
                           cwd=go_root, env=environment, check=True)
        # Some bundled Python runtimes require an additional SSL policy resource.
        # Preserve that policy when relocating the interpreter's compiled modules.
        ssl_policy = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
        if ssl_policy.is_file():
            shutil.copy2(ssl_policy, dist / ssl_policy.name)
        forbidden = set(modules) | {"license_keys.py", "private-key.pem"}
        if args.target == "workbench":
            forbidden.add("license_issuer.py")
        for path in dist.rglob("*"):
            if path.is_file() and path.name in forbidden:
                raise RuntimeError(f"编译产物包含不应交付的源文件或私钥：{path.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dist, destination)
    manifest = {"target": args.target, "platform": sys.platform,
                "sources": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in modules},
                "public_key_ids": sorted(keys),
                "executable_sha256": hashlib.sha256((destination / executable).read_bytes()).hexdigest()}
    if args.target == "workbench":
        manifest["web_resources"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sorted((destination / "web").iterdir()) if p.is_file()}
        manifest["pptx_component_sha256"] = hashlib.sha256(
            (destination / "pptx_extractor/bin/pptx-extractor.exe").read_bytes()).hexdigest()
        manifest["pptx_sources"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted((ROOT / "pptx_extractor").glob("*.go"))}
        manifest["customer_profile_sha256"] = (
            hashlib.sha256((ROOT / "customer-profile.json").read_bytes()).hexdigest()
            if (ROOT / "customer-profile.json").is_file() else None
        )
    (destination / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.target == "issuer" and sys.platform == "darwin":
        launcher = destination / "启动签发工具.command"
        launcher.write_text('#!/bin/zsh\ncd -- "${0:A:h}"\nexec ./AIOA-Issuer\n', encoding="utf-8")
        launcher.chmod(0o755)
    if args.target == "issuer":
        (destination / "使用说明.txt").write_text("""AIOA 授权签发工具 · 仅供管理员使用
1. Mac 双击“启动签发工具.command”；Windows 双击 AIOA-Issuer.exe。
2. 浏览器进入 http://127.0.0.1:54420/。首次设置密码并下载密钥备份 ZIP。
3. 私钥 private-key.pem 和密码仅由管理员保管并备份；public-keys.json 用于编译客户工作台。
4. 收到客户设备申请后，在签发台上传申请和加密私钥，填写密码、客户名称及有效期。
5. 有效期可选择天数或明确到期时间；下载 .license 文件交给客户。
6. 签发需要联网校时。关闭启动工具的终端窗口可退出。
已有密钥请直接使用，不要重新创建。不要把签发工具、密钥备份或密码交给客户。
""", encoding="utf-8")
    if args.target == "workbench":
        (destination / "使用说明.txt").write_text("""Office Auto Flow API · 编译授权版

二、启动工作台
双击 AIOA.exe 启动，使用右上角“退出工作台”正常退出。

三、首次配置与执行
1. 当前整合预览版全部模块开放，无需导入许可证。
2. 选择“PPTX 面辅料提取”，选择 PPTX 文件和一个 XLSX 模板。
3. 按需配置模板列匹配、字段别名和保存目录，然后开始运行。
4. 多个 PPTX 汇总到一个 XLSX；文件缺少原生表格时可能无法提取，当前不提供图片 OCR。
5. 配置登录地址，选择款号或批量 XLSX，选择 PDF 输出目录。
6. 辅料模板可选；选择模板后需指定辅料目录，批量任务汇总为一个 XLSX。
7. 更新前先点击“退出工作台”，等退出完成并关闭命令窗口后再安装。

四、数据保存位置
设置、授权、模板和登录会话保存在 %APPDATA%\\oa-login-cli；卸载保留上述数据及导出文件。
""", encoding="utf-8")
    print(f"已生成 {args.target} 编译产物：{destination}")


if __name__ == "__main__":
    main()
