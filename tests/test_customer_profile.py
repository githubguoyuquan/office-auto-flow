import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import customer_profile as profile
import oa_login_cli as app


class CustomerProfileTests(unittest.TestCase):
    def test_common_site_is_derived_from_login_and_customer_site_wins(self):
        with patch.object(profile, "PROFILE", dict(profile.DEFAULTS)):
            self.assertEqual(profile.business_url({"login_url": "https://oa.example.test/activiti/html/login.html"}),
                             "https://oa.example.test/activiti/html/plm/styleManage/newStyleManage.html")
        custom = dict(profile.DEFAULTS, business_url="https://customer.example.test/business.html")
        with patch.object(profile, "PROFILE", custom):
            self.assertEqual(profile.business_url({"login_url": "https://other.example.test/login"}),
                             custom["business_url"])

    def test_field_overrides_preserve_other_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "customer-profile.json"
            path.write_text(json.dumps({"fields": {"query_style": "materialNumber"}}))
            result = profile.load_profile(path)
            self.assertEqual(result["fields"]["query_style"], "materialNumber")
            self.assertEqual(result["fields"]["row_id"], "bomTitleId")
            self.assertEqual(profile.DEFAULTS["fields"]["query_style"], "styleCode")
            path.write_text(json.dumps({"fields": {"typo": "value"}}))
            with self.assertRaises(ValueError):
                profile.load_profile(path)
            path.write_text(json.dumps({"query_endpoint": "https://other.example.test/endpoint"}))
            with self.assertRaises(ValueError):
                profile.load_profile(path)

    def test_operation_kind_survives_customer_path_changes(self):
        self.assertTrue(app.is_process_sheet({"kind": "process_sheet", "url": "https://oa.example.test/custom"}))
        with patch.object(app, "business_url", return_value="https://oa.example.test/custom"):
            operation = app.business_operations({})[0]
        self.assertEqual(operation["url"], "https://oa.example.test/custom")
        self.assertTrue(app.is_process_sheet(operation))


if __name__ == "__main__":
    unittest.main()
