"""Connect signed release storage to workbench resources and status."""
from pathlib import Path
import re
import subprocess
import sys
import threading

from offline_updates import ALLOWED_FILES, BASE_API, UpdateError, UpdateStore
from ui_resources import WEB_ROOT, render_page
from update_keys import BASE_RELEASE_SEQUENCE, UPDATE_PUBLIC_KEYS


class UpdateManager:
    def __init__(self, directory, public_keys=None, base_sequence=None):
        self.directory = directory
        self.keys = dict(UPDATE_PUBLIC_KEYS if public_keys is None else public_keys)
        self.base_sequence = BASE_RELEASE_SEQUENCE if base_sequence is None else base_sequence
        if type(self.base_sequence) is not int or self.base_sequence < 0:
            raise UpdateError("基础版本序号无效。")
        self._cache = None
        self._cache_lock = threading.Lock()

    def store(self):
        return UpdateStore(Path(self.directory()) / "updates", self.keys)

    def selected(self):
        store = self.store()
        state = store.state()
        if state["active"] is None:
            return None, None
        root = store.root / "releases" / state["active"]
        with self._cache_lock:
            paths = [root, root / "web", root / "pptx_extractor", root / "pptx_extractor/bin",
                     root / "update.json", *(root / name for name in sorted(ALLOWED_FILES))]
            fingerprint = (str(root), tuple(
                (p.lstat().st_ino, p.lstat().st_mtime_ns, p.lstat().st_size, p.lstat().st_mode)
                if p.exists() or p.is_symlink() else None for p in paths))
            if self._cache is None or self._cache[0] != fingerprint:
                self._cache = (fingerprint, store.release(state["active"]))
            root, manifest = self._cache[1]
            return (root, manifest) if manifest["sequence"] > self.base_sequence else (None, None)

    def status(self):
        value = {"enabled": bool(self.keys), "base_api": BASE_API,
                 "minimum_sequence": self.base_sequence,
                 "version": "基础版本", "can_rollback": False, "error": ""}
        try:
            state = self.store().state()
            value["minimum_sequence"] = max(self.base_sequence, state["highest_sequence"])
            value["can_rollback"] = state["active"] is not None
            if state["active"] is not None:
                _, manifest = self.selected()
                if manifest is None:
                    value["can_rollback"] = False
                else:
                    value["version"] = manifest["version"]
        except (ValueError, OSError) as error:
            value["error"] = f"更新版本不可用，已使用基础版本：{error}"
        return value

    @staticmethod
    def healthcheck(root):
        page = render_page("update-self-test", root / "web")
        for marker in ("<!doctype html>", "const TOKEN = 'update-self-test'", "refresh();"):
            if marker not in page:
                raise UpdateError("更新页面不完整，自检失败。")
        from playwright._impl._driver import compute_driver_executable
        node, _ = compute_driver_executable()
        scripts = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
        if len(scripts) != 2:
            raise UpdateError("更新页面脚本结构不兼容。")
        try:
            check = subprocess.run([str(node), "--check"], input="\n".join(scripts),
                                   text=True, encoding="utf-8", capture_output=True, timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError("更新页面自检未完成，请重试或重新安装基础程序。") from error
        if check.returncode:
            raise UpdateError("更新页面脚本存在语法错误，已保留当前版本。")
        component = root / "pptx_extractor/bin/pptx-extractor.exe"
        if component.exists():
            if sys.platform != "win32":
                raise UpdateError("含 Windows 提取组件的更新包只能在 Windows 中安装。")
            with component.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise UpdateError("提取组件文件格式无效。")
            import tempfile
            from openpyxl import Workbook
            from pptx_service import invoke
            try:
                with tempfile.TemporaryDirectory(prefix="oaf-component-check-") as temporary:
                    template = Path(temporary) / "template.xlsx"
                    workbook = Workbook()
                    try:
                        workbook.active.append(["款号"])
                        workbook.save(template)
                    finally:
                        workbook.close()
                    result = invoke({"action": "analyze_template", "template": str(template), "rules": []},
                                    threading.Event(), lambda event: None, binary=component, timeout=20)
                    if result.get("headers") != ["款号"]:
                        raise ValueError("Unexpected template response")
            except Exception as error:
                raise UpdateError("新提取组件自检失败，已保留当前版本。") from error

    def page(self, token):
        try:
            root, _ = self.selected()
            if root is not None:
                return render_page(token, root / "web")
        except (ValueError, OSError):
            pass
        return render_page(token, WEB_ROOT)

    def component(self, builtin):
        try:
            root, _ = self.selected()
            if root is not None and sys.platform == "win32":
                candidate = root / "pptx_extractor/bin/pptx-extractor.exe"
                if candidate.is_file():
                    return candidate
        except (ValueError, OSError):
            pass
        return builtin

    def install(self, package):
        if not self.keys:
            raise UpdateError("此版本尚未配置更新发布公钥，请先安装支持更新的正式基础版本。")
        self.store().install(package, self.healthcheck, minimum_sequence=self.base_sequence)
        return self.status()

    def rollback(self):
        self.store().rollback(self.healthcheck)
        return self.status()
