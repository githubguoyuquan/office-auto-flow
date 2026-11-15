from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook, load_workbook

import oa_login_cli as app
from material_suppliers import MaterialSupplierLookup, validate_supplier_settings


def response(rows, pages=1):
    return {"code": 10000, "data": {"pageCount": pages, "list": rows}}


class SupplierQueryTests(unittest.TestCase):
    def test_confirmed_request_preserves_supplier_codes_and_caches(self):
        post = Mock(return_value=response([
            {"materialCode": "TTX3240", "supplier1": "160002", "supplier2": ""}]))
        lookup = MaterialSupplierLookup(post)
        self.assertEqual(lookup("TTX3240"), ("160002", ""))
        self.assertEqual(lookup("TTX3240"), ("160002", ""))
        post.assert_called_once()
        endpoint, body = post.call_args.args
        self.assertEqual(endpoint, "material/queryMaterialListWithPage")
        self.assertEqual(body, {
            "pageNum": 1, "pageSize": 10, "dataType": "Z200", "matnr": "", "maktx": "",
            "zzitemH1": "", "zzitemCd": "", "materialQuality": "", "supplementDesc": "",
            "managementType": "", "creator": "", "supplier": "", "supplierCode": "",
            "materialCode": "TTX3240", "syncSapStatus": "",
        })

    def test_exact_match_can_be_on_later_page(self):
        post = Mock(side_effect=[response([
            {"materialCode": "001-extra", "supplier1": "wrong"}], 2), response([
            {"materialCode": "001", "supplier1": "000007", "supplier2": "000008"}], 2)])
        self.assertEqual(MaterialSupplierLookup(post)("001"), ("000007", "000008"))
        self.assertEqual([call.args[1]["pageNum"] for call in post.call_args_list], [1, 2])

    def test_conflicting_exact_records_are_rejected(self):
        post = Mock(return_value=response([
            {"materialCode": "001", "supplier1": "A"},
            {"materialCode": "001", "supplier1": "B"}]))
        with self.assertRaisesRegex(ValueError, "供应商不一致"):
            MaterialSupplierLookup(post)("001")

    def test_no_match_does_not_reuse_a_partial_match(self):
        post = Mock(return_value=response([{"materialCode": "001-extra", "supplier1": "wrong"}]))
        self.assertEqual(MaterialSupplierLookup(post)("001"), ("", ""))
        self.assertEqual(MaterialSupplierLookup(Mock(return_value=response([], 0)))("001"), ("", ""))

    def test_invalid_schema_pagination_and_supplier_types_fail(self):
        invalid = [
            {"code": 20000, "message": "物料分类不能为空"},
            {"code": 10000, "data": {"list": {}}},
            response([{"supplier1": "A"}]),
            response([{"materialCode": "001", "supplier1": {"id": "A"}}]),
            response([], True), response([], 201), response([], 2),
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                MaterialSupplierLookup(Mock(return_value=payload))("001")

    def test_customer_request_overrides_are_isolated(self):
        settings = validate_supplier_settings({"request_defaults": {"dataType": "OTHER"}})
        self.assertEqual(settings["request_defaults"]["dataType"], "OTHER")
        self.assertEqual(validate_supplier_settings({})["request_defaults"]["dataType"], "Z200")
        with self.assertRaises(ValueError):
            validate_supplier_settings({"endpoint": "https://other.example.test/query"})


class SupplierWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "templates").mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        book = Workbook()
        book.active.append(["辅料编号", "名称"])
        book.save(self.root / "templates" / "test.xlsx")
        book.close()
        patcher = patch.object(app, "data_dir", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.operation = {
            "name": "工艺单打印", "kind": "process_sheet", "style_code": "STYLE1",
            "style_codes": ["STYLE1"], "url": "https://oa.example.test/activiti/html/plm/styleManage/newStyleManage.html",
            "print_settings": {"template_id": "test", "converted_dir": str(self.output)},
        }
        self.page = Mock()
        self.page.url = self.operation["url"]
        self.page.evaluate.return_value = {}
        self.page.goto.side_effect = lambda url, **_: setattr(self.page, "url", url)
        self.context = Mock()
        self.context.pages = [self.page]
        self.context.cookies.return_value = []
        self.controller = app.AutomationController()
        self.controller._on_login_page = Mock(return_value=False)

    @staticmethod
    def http_result(url, payload):
        return SimpleNamespace(status=200, ok=True, url=url, json=lambda: payload)

    def assert_export(self):
        files = list(self.output.glob("*.xlsx"))
        self.assertEqual(len(files), 1)
        book = load_workbook(files[0])
        try:
            self.assertEqual([book.active.cell(1, col).value for col in range(1, 5)],
                             ["辅料编号", "名称", "供应商", "供应商2"])
            self.assertEqual([book.active.cell(2, col).value for col in range(1, 5)],
                             ["TTX3240", "辅料", "160002", "000003"])
        finally:
            book.close()

    def test_single_print_calls_supplier_api_and_exports_the_same_row(self):
        def post(url, **kwargs):
            if url.endswith("queryProcessSheet"):
                payload = response([{"styleCode": "STYLE1", "bomTitleId": "ID1", "seqNo": "00"}])
            elif url.endswith("printProcessSheet"):
                payload = {"code": 10000, "data": {"bomTitle": {"style_code": "STYLE1"}}}
            else:
                self.assertEqual(kwargs["data"]["materialCode"], "TTX3240")
                self.assertEqual(kwargs["data"]["dataType"], "Z200")
                payload = response([{"materialCode": "TTX3240", "supplier1": "160002", "supplier2": "000003"}])
            return self.http_result(url, payload)
        self.context.request.post.side_effect = post
        tables = [[['辅料编号', 'TTX3240'], ['名称', '辅料']]]
        with patch.object(app, "save_process_sheet_pdf", return_value=(self.root / "print.pdf", "", tables)):
            self.controller._print_process_sheets(self.context, self.page, self.operation)
        self.assert_export()

    def test_batch_aggregate_uses_the_primary_context_for_suppliers(self):
        def run(job, context, publish):
            job["_batch_trim"].append(("STYLE1", 0, [{"辅料编号": "TTX3240", "名称": "辅料"}]))
            return {"phase": "done", "content": "PDF saved"}
        self.controller._run = run
        self.context.request.post.side_effect = lambda url, **_: self.http_result(url, response([
            {"materialCode": "TTX3240", "supplier1": "160002", "supplier2": "000003"}]))
        with patch.object(app, "open_business_page"):
            self.controller._run_batch(self.operation, self.context, None)
        self.assert_export()
        self.context.request.post.assert_called_once()
        self.assertEqual(self.controller.snapshot()["phase"], "done")

    def test_cancelled_partial_batch_keeps_basic_export_without_new_requests(self):
        def run(job, context, publish):
            job["_batch_trim"].append(("STYLE1", 0, [{"辅料编号": "TTX3240", "名称": "辅料"}]))
            return {"phase": "cancelled", "message": "已停止"}
        self.controller._run = run
        with patch.object(app, "open_business_page"):
            self.controller._run_batch(self.operation, self.context, None)
        self.context.request.post.assert_not_called()
        self.assertEqual(len(list(self.output.glob("*.xlsx"))), 1)
        self.assertIn("供应商信息尚未补充", self.controller.snapshot()["content"])

    def test_expired_parallel_request_stops_without_opening_login(self):
        self.context.request.post.return_value = SimpleNamespace(status=401, url=self.operation["url"])
        operation = dict(self.operation, batch_isolated=True,
            _batch_auth_failure=threading.Event(), _batch_abort=threading.Event())
        with self.assertRaisesRegex(RuntimeError, "登录已失效"):
            self.controller._post_oa_json(self.context, self.page, operation, "material/query", {})
        self.assertTrue(operation["_batch_auth_failure"].is_set())
        self.page.goto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
