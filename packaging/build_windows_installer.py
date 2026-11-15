#!/usr/bin/env python3
"""Build the per-user Windows API workbench installer with NSIS."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAYLOAD = ROOT / "preview" / "Office-Auto-Flow-API-67b91cf"
DEFAULT_OUTPUT = ROOT / "preview" / "Office-Auto-Flow-API-Setup-67b91cf-win-x64.exe"


def make_icon(destination: Path) -> None:
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 247, 247), radius=55, fill=(33, 33, 33, 255))
    draw.line(((59, 183), (126, 63), (195, 183)), fill=(239, 239, 239, 255), width=20, joint="curve")
    draw.rounded_rectangle((92, 132, 164, 149), radius=8, fill=(112, 218, 181, 255))
    image.save(destination, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def update_user_guide(path: Path, compiled: bool = False) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index("二、启动工作台")
    end = text.index("三、首次配置与执行", start)
    install_steps = """二、安装和启动
1. 双击安装程序，按提示完成安装。默认安装到当前 Windows 用户的本地程序目录，无需管理员权限。
2. 双击桌面的“AIOA API 工作台”图标启动，也可在开始菜单中找到该入口。
3. 浏览器会打开本机工作台 http://127.0.0.1:54418/。运行期间请保持命令窗口打开；关闭该窗口即可退出。
4. 工作台默认使用深色界面，可通过右上角按钮切换浅色或深色，选择会被记住。
5. 如果提示端口已被占用，先关闭之前启动的工作台，再重新打开桌面图标。
6. 可在 Windows“设置 → 应用”或开始菜单中卸载。卸载会保留工作台配置、浏览器会话和导出文件。
7. 更新安装前，请先关闭工作台及其命令窗口。若仍提示文件无法写入，请联系 IT 检查目录权限或安全软件占用。

"""
    if compiled:
        install_steps = install_steps.replace("关闭该窗口即可退出。", "请使用工作台右上角“退出工作台”正常退出。")
    path.write_text(text[:start] + install_steps + text[end:], encoding="utf-8")


def nsis_path(path: str) -> str:
    return path.replace("$", "$$").replace('"', '$\\"')


def build_script(payload: Path, output: Path, version: str) -> str:
    compiled = (payload / "AIOA.exe").is_file()
    launcher = "AIOA.exe" if compiled else "start_oa_login.bat"
    cleanup = '''  Delete "$INSTDIR\\oa_login_cli.py"
  Delete "$INSTDIR\\trim_template.py"
  Delete "$INSTDIR\\offline_license.py"
  Delete "$INSTDIR\\license_manager.py"
  Delete "$INSTDIR\\license_keys.py"
  Delete "$INSTDIR\\start_oa_login.ps1"
  Delete "$INSTDIR\\start_oa_login.bat"
  RMDir /r "$INSTDIR\\runtime"
''' if compiled else ""
    files = sorted(p for p in payload.rglob("*") if p.is_file())
    dirs = sorted((p for p in payload.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
    remove_files = "\n".join('  Delete "$INSTDIR\\' + nsis_path(str(p.relative_to(payload)).replace("/", "\\")) + '"' for p in files)
    remove_dirs = "\n".join('  RMDir "$INSTDIR\\' + nsis_path(str(p.relative_to(payload)).replace("/", "\\")) + '"' for p in dirs)
    payload_path = nsis_path(str(payload).replace("/", "\\"))
    output_path = nsis_path(str(output))
    return f'''Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
Name "Office Auto Flow API"
OutFile "{output_path}"
InstallDir "$LOCALAPPDATA\\Programs\\Office Auto Flow API"
RequestExecutionLevel user
SetCompressor /SOLID lzma
CRCCheck on
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "0.1.0.0"
VIAddVersionKey /LANG=2052 "ProductName" "Office Auto Flow API"
VIAddVersionKey /LANG=2052 "FileDescription" "Office Auto Flow API 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "{version}"
VIAddVersionKey /LANG=2052 "LegalCopyright" ""
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\\{launcher}"
!define MUI_FINISHPAGE_RUN_TEXT "启动工作台"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  SetShellVarContext current
  ${{IfNot}} ${{RunningX64}}
    MessageBox MB_ICONSTOP "此版本需要 64 位 Windows。"
    Abort
  ${{EndIf}}
  ${{IfNot}} ${{AtLeastWin10}}
    MessageBox MB_ICONSTOP "此版本需要 Windows 10 或 Windows 11。"
    Abort
  ${{EndIf}}
FunctionEnd

Section "安装工作台" SEC_MAIN
  # Check the two files most likely to be locked by a running workbench before
  # extracting anything, so an update does not leave a partial installation.
  StrCpy $R1 "$INSTDIR\\AIOA.exe"
  Call CheckWritableRuntime
  StrCpy $R1 "$INSTDIR\\runtime\\python.exe"
  Call CheckWritableRuntime
  StrCpy $R1 "$INSTDIR\\runtime\\_asyncio.pyd"
  Call CheckWritableRuntime
  SetOutPath "$INSTDIR"
  SetOverwrite on
  File /r "{payload_path}\\*"
{cleanup}
  WriteUninstaller "$INSTDIR\\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\\Office Auto Flow API"
  CreateShortCut "$DESKTOP\\AIOA API 工作台.lnk" "$INSTDIR\\{launcher}" "" "$INSTDIR\\app.ico" 0 SW_SHOWNORMAL "" "启动工艺单自动化工作台"
  CreateShortCut "$SMPROGRAMS\\Office Auto Flow API\\AIOA API 工作台.lnk" "$INSTDIR\\{launcher}" "" "$INSTDIR\\app.ico"
  CreateShortCut "$SMPROGRAMS\\Office Auto Flow API\\卸载.lnk" "$INSTDIR\\Uninstall.exe"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI" "DisplayName" "Office Auto Flow API"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI" "DisplayVersion" "{version}"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI" "DisplayIcon" "$INSTDIR\\app.ico"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI" "UninstallString" '"$INSTDIR\\Uninstall.exe"'
  WriteRegDWORD HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI" "NoModify" 1
  WriteRegDWORD HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI" "NoRepair" 1
SectionEnd

Function CheckWritableRuntime
  IfFileExists "$R1" 0 writable_done
writable_retry:
  System::Call 'kernel32::CreateFileW(w "$R1", i 0x40000000, i 0, p 0, i 3, i 0x80, p 0) p .r0'
  StrCmp $0 -1 writable_blocked
  System::Call 'kernel32::CloseHandle(p r0)'
  Goto writable_done
writable_blocked:
  MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "无法写入已有的工作台文件：$R1$\\r$\\n请关闭工作台和黑色命令窗口后选择重试。若仍失败，请检查目录权限或安全软件是否占用。" IDRETRY writable_retry
  Abort
writable_done:
FunctionEnd

Function un.onInit
  SetShellVarContext current
FunctionEnd

Section "Uninstall"
  Delete "$DESKTOP\\AIOA API 工作台.lnk"
  Delete "$SMPROGRAMS\\Office Auto Flow API\\AIOA API 工作台.lnk"
  Delete "$SMPROGRAMS\\Office Auto Flow API\\卸载.lnk"
  RMDir "$SMPROGRAMS\\Office Auto Flow API"
{remove_files}
  Delete "$INSTDIR\\Uninstall.exe"
{remove_dirs}
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfficeAutoFlowAPI"
SectionEnd
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, default=DEFAULT_PAYLOAD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--makensis", required=True, type=Path)
    parser.add_argument("--nsis-dir", required=True, type=Path)
    parser.add_argument("--version", default="0.1.0-preview.67b91cf")
    args = parser.parse_args()
    payload, output = args.payload.resolve(), args.output.resolve()
    compiled = (payload / "AIOA.exe").is_file()
    if not compiled and not (payload / "runtime" / "python.exe").is_file():
        parser.error("payload 缺少 Windows Python 运行时")
    if compiled:
        manifest = json.loads((payload / "build-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("target") != "workbench" or manifest.get("platform") != "win32" or not manifest.get("public_key_ids"):
            parser.error("编译产物的平台或公钥配置不正确")
        for name in ("oa_login_cli.py", "trim_template.py", "offline_license.py", "license_manager.py", "customer_profile.py", "material_suppliers.py"):
            if manifest.get("sources", {}).get(name) != hashlib.sha256((ROOT / name).read_bytes()).hexdigest():
                parser.error(f"编译产物与当前源代码不一致：{name}")
        profile_path = ROOT / "customer-profile.json"
        profile_hash = hashlib.sha256(profile_path.read_bytes()).hexdigest() if profile_path.is_file() else None
        if manifest.get("customer_profile_sha256") != profile_hash:
            parser.error("编译产物的客户配置与当前分支不一致")
        if profile_path.is_file() and (not (payload / profile_path.name).is_file()
                or hashlib.sha256((payload / profile_path.name).read_bytes()).hexdigest() != profile_hash):
            parser.error("编译产物缺少正确的客户配置")
        if manifest.get("executable_sha256") != hashlib.sha256((payload / "AIOA.exe").read_bytes()).hexdigest():
            parser.error("编译程序校验失败")
        for path in payload.rglob("*"):
            if path.is_file() and path.name in {"oa_login_cli.py", "license_issuer.py", "private-key.pem", "offline_license.py", "license_manager.py", "license_keys.py", "trim_template.py", "customer_profile.py", "material_suppliers.py"}:
                parser.error("客户安装包不得包含应用源码、签发工具或私钥")
    elif (payload / "oa_login_cli.py").read_bytes() != (ROOT / "oa_login_cli.py").read_bytes():
        parser.error("payload 的应用代码与当前分支不一致")
    if output.exists():
        parser.error(f"输出文件已存在，请指定新路径：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aioa-windows-installer-") as temporary:
        temp = Path(temporary)
        staged = temp / "payload"
        shutil.copytree(payload, staged)
        make_icon(staged / "app.ico")
        update_user_guide(staged / "使用说明.txt", compiled=compiled)
        source = temp / "installer.nsi"
        source.write_text(build_script(staged, output, args.version), encoding="utf-8-sig")
        environment = dict(os.environ, NSISDIR=str(args.nsis_dir.resolve()))
        subprocess.run([str(args.makensis.resolve()), "-V2", str(source)], env=environment, check=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"安装程序：{output}\nSHA-256：{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
