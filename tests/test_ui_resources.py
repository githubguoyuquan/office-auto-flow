from pathlib import Path
import shutil
import tempfile
import unittest

from ui_resources import WEB_ROOT, render_page


class UIResourceTests(unittest.TestCase):
    def test_reloads_resources_and_injects_current_session_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "web"
            shutil.copytree(WEB_ROOT, root)
            page = render_page("session-one", root)
            self.assertIn("const TOKEN = 'session-one'", page)
            self.assertNotIn("__PPTX_UI__", page)
            self.assertNotIn("__WORKBENCH_JS__", page)
            with (root / "workbench.css").open("a") as stream:
                stream.write("/* updated-resource */")
            updated = render_page("session-two", root)
            self.assertIn("updated-resource", updated)
            self.assertIn("const TOKEN = 'session-two'", updated)
            self.assertNotIn("session-one", updated)

    def test_missing_or_invalid_resource_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "web"
            shutil.copytree(WEB_ROOT, root)
            (root / "pptx.js").unlink()
            with self.assertRaisesRegex(ValueError, "界面文件缺失或损坏"):
                render_page("session", root)
            (root / "pptx.js").write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "界面文件缺失或损坏"):
                render_page("session", root)
