"""Load independently packaged workbench resources from the application directory."""
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent / "web"
RESOURCE_NAMES = ("index.html", "workbench.css", "bootstrap.js", "workbench.js", "pptx.js", "template_builder.js")


def render_page(token: str, root: Path | None = None) -> str:
    root = WEB_ROOT if root is None else root
    try:
        resources = {name: (root / name).read_text(encoding="utf-8") for name in RESOURCE_NAMES}
    except (OSError, UnicodeError) as error:
        raise ValueError("工作台界面文件缺失或损坏，请重新安装或恢复更新前版本。") from error
    script = resources["workbench.js"].replace("__PPTX_UI__", resources["pptx.js"])
    script = script.replace("__TEMPLATE_BUILDER_UI__", resources["template_builder.js"])
    return (resources["index.html"]
            .replace("__WORKBENCH_CSS__", resources["workbench.css"])
            .replace("__BOOTSTRAP_JS__", resources["bootstrap.js"])
            .replace("__WORKBENCH_JS__", script)
            .replace("__AIOA_TOKEN__", token))
