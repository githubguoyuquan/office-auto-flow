import base64
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import oa_login_cli as app
from license_issuer import create_keys, issue_license
from license_manager import LicenseManager
from offline_license import TrustedClock


class HTTPAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.private, public = create_keys("test-password-only")
        keys = {name: base64.b64decode(value) for name, value in public.items()}
        self.manager = LicenseManager(lambda: self.root, keys, TrustedClock(lambda: 1800000000), "AIOA-" + "B" * 64)
        controller = app.AutomationController()
        controller.worker = Mock()
        controller.worker.is_alive.return_value = True
        for name, value in (("LICENSES", self.manager), ("CONTROLLER", controller), ("data_dir", lambda: self.root)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.LocalHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["X-AIOA-Token"] = app.ACCESS_TOKEN
        request = Request(f"http://127.0.0.1:{self.server.server_port}" + path,
                          data=json.dumps(body).encode(), headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def document(self):
        request = {"version": 1, "product": "office-auto-flow-api",
                   "device_id": self.manager.device, "request_id": "b" * 32}
        return issue_license(request, self.private, "test-password-only", "HTTP test", days=7,
                             now=1800000000).decode()

    def test_export_and_import_require_token(self):
        self.assertEqual(self.post("/api/license/request", {}, False)[0], 403)
        self.assertEqual(self.post("/api/license/import", {"document": self.document()}, False)[0], 403)
        code, value = self.post("/api/license/request", {})
        self.assertEqual(code, 200)
        self.assertEqual(value["request"]["device_id"], self.manager.device)

    def test_task_endpoint_requires_license(self):
        self.assertEqual(self.post("/api/settings", {"login_url": "https://oa.example.test/activiti/html/login.html"})[0], 200)
        self.assertEqual(self.post("/api/print-settings", {"original_dir": str(self.root / "pdf"), "template_id": ""})[0], 200)
        body = {"name": "工艺单打印", "style_code": "TEST123"}
        code, error = self.post("/api/run", body)
        self.assertEqual(code, 400)
        self.assertIn("授权", error["error"])
        self.assertFalse(app.CONTROLLER.task_active)
        code, result = self.post("/api/license/import", {"document": self.document()})
        self.assertEqual(code, 200, result)
        self.assertTrue(result["license"]["valid"])
        self.assertEqual(self.post("/api/run", body)[0], 200)
        self.assertEqual(app.CONTROLLER.jobs.get_nowait()["style_code"], "TEST123")
        self.assertEqual(self.post("/api/license/import", {"document": self.document()})[0], 400)


if __name__ == "__main__":
    unittest.main()
