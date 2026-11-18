from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from openpyxl import Workbook, load_workbook

import oa_login_cli as app
from material_suppliers import MaterialSupplierLookup, supplier_names
from pptx_service import ExtractionCancelled
from pptx_suppliers import extract_with_suppliers, fill_suppliers, supplier_columns


class PptxSupplierTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cancel = threading.Event()

    def workbook(self, headers, rows, name="data.xlsx"):
        path = self.root / name
        book = Workbook()
        book.active.append(headers)
        for row in rows:
            book.active.append(row)
        book.save(path)
        book.close()
        return path

    def test_fill_names_two_slots_cache_and_remove_internal_code(self):
        path = self.workbook(["供应商", "供应商1", "供应商2", "__internal"],
                             [[None, None, None, "TRIM1"], [None, None, None, "TRIM1"],
                              [None, None, None, "TRIM2"], ["OLD_SHOULD_CLEAR", None, None, None]])
        post = Mock(side_effect=lambda endpoint, request: {"code": 10000, "data": {"list": [
            {"materialCode": request["materialCode"], "supplier1": "0001", "supplier2": "0002"}], "pageCount": 1}})
        api = MaterialSupplierLookup(post)
        result = fill_suppliers(path, 4, supplier_columns(["供应商", "供应商1", "供应商2"]),
                                lambda code: supplier_names(api(code), {"0001": "甲供应商"}), self.cancel)
        self.assertEqual(result["supplier_queries"], 2)
        self.assertEqual(post.call_count, 2)
        book = load_workbook(path)
        try:
            rows = list(book.active.values)
            self.assertEqual(rows[0], ("供应商", "供应商1", "供应商2"))
            self.assertEqual(rows[1], ("甲供应商 / 0002", "甲供应商", "0002"))
            self.assertEqual(rows[2], rows[1])
            self.assertEqual(rows[-1], (None, None, None))
        finally:
            book.close()

    def test_lookup_error_or_cancel_does_not_save_partial_values(self):
        path = self.workbook(["供应商", "__internal"], [[None, "TRIM1"], [None, "TRIM2"]])
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            fill_suppliers(path, 2, {1: -1}, Mock(side_effect=[("甲", ""), ValueError("test error")]), self.cancel)
        self.assertEqual(path.read_bytes(), original)
        self.cancel.set()
        with self.assertRaises(ExtractionCancelled):
            fill_suppliers(path, 2, {1: -1}, Mock(), self.cancel)
        self.assertEqual(path.read_bytes(), original)

    def test_no_supplier_columns_does_not_run_oa_stage(self):
        request = {"action": "extract", "template": "template.xlsx", "inputs": ["source.pptx"]}
        stage = Mock()
        result = {"output": "result.xlsx"}
        with patch("pptx_suppliers.invoke", side_effect=[{"headers": ["辅料编号"]}, result]):
            self.assertEqual(extract_with_suppliers(request, self.cancel, Mock(), "kernel", stage), result)
        stage.assert_not_called()

    def test_staged_lookup_works_when_material_code_is_not_in_template(self):
        template = self.workbook(["供应商"], [], "template.xlsx")
        original = template.read_bytes()
        request = {"action": "extract", "template": str(template), "inputs": ["source.pptx"], "output_dir": str(self.root)}

        def fake_invoke(request, *args, **options):
            if request["action"] == "analyze_template":
                return {"headers": ["供应商"], "mapping": [-1], "fields": ["辅料编号"]}
            book = load_workbook(request["template"])
            self.assertEqual(list(book.active.values)[0], ("供应商", "__OAF_LOOKUP_MATERIAL__"))
            book.active.append([None, "TRIM1"])
            output = Path(request["output_dir"]) / "raw.xlsx"
            book.save(output)
            book.close()
            return {"output": str(output), "success": 1, "failed": 0, "count": 1}

        def stage(path, code_column, columns):
            return fill_suppliers(path, code_column, columns, lambda code: ("甲供应商", "乙供应商"), self.cancel)

        with patch("pptx_suppliers.invoke", side_effect=fake_invoke):
            result = extract_with_suppliers(request, self.cancel, Mock(), "kernel", stage)
        self.assertEqual(template.read_bytes(), original)
        book = load_workbook(result["output"])
        try:
            self.assertEqual(list(book.active.values), [("供应商",), ("甲供应商 / 乙供应商",)])
        finally:
            book.close()
        self.assertEqual(list(self.root.glob(".pptx-suppliers-*")), [])

    def test_existing_auth_transport_is_reused_and_login_only_after_rejection(self):
        controller = app.AutomationController()
        page = Mock()
        page.is_closed.return_value = False
        context = Mock()
        context.pages = [page]
        context.cookies.return_value = []
        response = Mock(status=200, ok=True, url="http://oa.example/activiti/material/queryMaterialListWithPage")
        response.json.return_value = {"code": 10000, "data": {"list": [{"materialCode": "TRIM1", "supplier1": "01"}], "pageCount": 1}}
        context.request.post.return_value = response
        operation = {"url": "http://oa.example/activiti/html/index.html", "login_url": "http://oa.example/activiti/html/login.html",
                     "supplier_name_mapping": {"01": "甲"}, "_supplier_action": lambda lookup: lookup("TRIM1")}
        self.assertEqual(controller._finish_pptx_suppliers(context, operation), ("甲", ""))
        page.goto.assert_not_called()
        rejected = Mock(status=401)
        context.request.post.side_effect = [rejected, response]
        with patch.object(controller, "_wait_for_login") as login:
            self.assertEqual(controller._finish_pptx_suppliers(context, operation), ("甲", ""))
            login.assert_called_once()
        page.goto.assert_called_once()

    def test_supplier_stage_reuses_browser_worker_and_keeps_local_task_locked(self):
        controller = app.AutomationController()
        controller.task_active = True
        controller.state["operation"] = "PPTX 面辅料提取"
        controller.local_worker = threading.current_thread()
        page = Mock()
        page.is_closed.return_value = False
        context = Mock()
        context.pages = [page]
        context.cookies.return_value = []
        threads = []

        def response(*args, **options):
            threads.append(threading.current_thread())
            result = Mock(status=200, ok=True, url="http://oa.example/activiti/material/queryMaterialListWithPage")
            result.json.return_value = {"code": 10000, "data": {"list": [{"materialCode": "TRIM1", "supplier1": "01"}], "pageCount": 1}}
            return result

        context.request.post.side_effect = response
        playwright = MagicMock()
        with patch("playwright.sync_api.sync_playwright", return_value=playwright), \
                patch.object(app, "launch_context", return_value=context) as launch, \
                patch.object(app, "load_settings", return_value={"login_url": "http://oa.example/activiti/html/login.html"}), \
                patch.object(app, "load_supplier_mapping", return_value={"entries": {"01": "甲"}}), \
                patch.object(app, "data_dir", return_value=self.root):
            try:
                for index in range(2):
                    path = self.workbook(["供应商", "__internal"], [[None, "TRIM1"]], f"job-{index}.xlsx")
                    self.assertEqual(controller._pptx_supplier_stage(path, 2, {1: -1})["supplier_queries"], 1)
                    self.assertTrue(controller.task_active)
                    with self.assertRaises(ValueError):
                        controller.reset()
                launch.assert_called_once()
                self.assertTrue(all(thread is controller.worker for thread in threads))
                page.goto.assert_not_called()
            finally:
                controller.local_worker = None
                controller.task_active = False
                controller.shutdown()
