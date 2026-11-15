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
    @staticmethod
    def mapping_body(name="供应商甲"):
        import io
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(["供应商编号", "供应商名称"])
        book.active.append([160002, name])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        return {"filename": "供应商信息表.xlsx", "content": base64.b64encode(stream.getvalue()).decode()}

    def test_supplier_mapping_configuration_upload_update_and_clear(self):
        from material_suppliers import load_supplier_mapping
        body = self.mapping_body()
        self.assertEqual(self.post("/api/supplier-mapping", body, False)[0], 403)
        code, result = self.post("/api/supplier-mapping", body)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["supplier_mapping"]["count"], 1)
        self.assertEqual(self.post("/api/supplier-mapping", self.mapping_body("供应商乙"))[0], 200)
        self.assertEqual(load_supplier_mapping(self.root)["entries"], {"160002": "供应商乙"})
        self.assertEqual(self.post("/api/supplier-mapping", {"filename": "bad.xlsx", "content": "invalid"})[0], 400)
        missing = self.mapping_body()
        import io
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(base64.b64decode(missing["content"])))
        book.active["A2"].value = None
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        missing["content"] = base64.b64encode(stream.getvalue()).decode()
        code, error = self.post("/api/supplier-mapping", missing)
        self.assertEqual(code, 400)
        self.assertIn("第 2 行缺少供应商编号", error["error"])
        self.assertIn("重新上传", error["error"])
        self.assertEqual(load_supplier_mapping(self.root)["entries"], {"160002": "供应商乙"})
        code, result = self.post("/api/supplier-mapping/clear", {})
        self.assertEqual(code, 200, result)
        self.assertFalse(result["supplier_mapping"]["configured"])
        self.assertEqual(load_supplier_mapping(self.root)["entries"], {})

    def test_running_task_blocks_supplier_mapping_updates(self):
        app.CONTROLLER.task_active = True
        self.assertEqual(self.post("/api/supplier-mapping", self.mapping_body())[0], 400)
        self.assertEqual(self.post("/api/supplier-mapping/clear", {})[0], 400)

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

    def test_business_api_cannot_override_the_registered_module(self):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from offline_license import canonical_payload
        value = json.loads(self.document())
        value["payload"]["features"] = ["future_business"]
        key = load_pem_private_key(self.private, password=b"test-password-only")
        value["signature"] = base64.b64encode(key.sign(canonical_payload(value["payload"]))).decode()
        code, result = self.post("/api/license/import", {"document": json.dumps(value)})
        self.assertEqual(code, 200, result)
        self.assertTrue(result["license"]["valid"])
        self.assertEqual(result["license"]["features"], ["future_business"])
        self.post("/api/print-settings", {"original_dir": str(self.root / "pdf"), "template_id": ""})
        operations = [
            {"name": "工艺单打印", "kind": "process_sheet", "module_id": "process_sheet",
             "url": "https://oa.example.test/activiti/html/plm/styleManage/newStyleManage.html"},
            {"name": "未来测试业务", "module_id": "future_business", "url": "https://oa.example.test/other"},
        ]
        with patch.object(app, "business_operations", return_value=operations):
            code, result = self.post("/api/run", {"name": "工艺单打印", "style_code": "TEST123", "module_id": "future_business"})
            self.assertEqual(code, 400)
            self.assertIn("不包含此功能", result["error"])
            self.assertFalse(app.CONTROLLER.task_active)
            self.assertEqual(self.post("/api/run", {"name": "未来测试业务", "module_id": "process_sheet"})[0], 200)
        self.assertEqual(app.CONTROLLER.jobs.get_nowait()["module_id"], "future_business")

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
