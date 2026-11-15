import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import offline_license as core
from license_issuer import create_keys, issue_license
from license_manager import LicenseManager


class LicensingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private, encoded = create_keys("test-password-only")
        cls.keys = {name: base64.b64decode(value) for name, value in encoded.items()}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = 1800000000
        self.elapsed = 0
        self.wall = 0
        self.fetch = Mock(side_effect=lambda: self.base + self.elapsed)
        self.clock = core.TrustedClock(self.fetch, lambda: self.elapsed, lambda: self.wall)
        self.device = "AIOA-" + "A" * 64
        self.request = dict(version=1, product=core.PRODUCT, device_id=self.device, request_id="a" * 32)
        self.manager = LicenseManager(lambda: Path(self.temp.name), self.keys, self.clock, self.device)

    def issue(self, **kwargs):
        return issue_license(self.request, self.private, "test-password-only", "测试客户",
                             now=self.base, **kwargs)

    def module_license(self, features):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        value = json.loads(self.issue(days=7))
        value["payload"]["features"] = features
        key = load_pem_private_key(self.private, password=b"test-password-only")
        value["signature"] = base64.b64encode(key.sign(core.canonical_payload(value["payload"]))).decode()
        return json.dumps(value).encode()

    def test_module_permissions_are_independent_of_license_validity(self):
        self.manager.install(self.module_license(["future_business"]))
        self.assertTrue(self.manager.status()["valid"])
        self.assertEqual(self.manager.status()["features"], ["future_business"])
        self.manager.require_valid(feature="future_business")
        with self.assertRaises(core.LicenseError):
            self.manager.require_valid(feature="process_sheet")
        self.manager.install(self.issue(days=7))
        self.manager.require_valid(feature="process_sheet")
        with self.assertRaises(core.LicenseError):
            self.manager.require_valid(feature="future_business")

    def test_module_tampering_or_duplicate_grants_are_rejected(self):
        original = self.issue(days=7)
        self.manager.install(original)
        value = json.loads(original)
        value["payload"]["features"].append("future_business")
        with self.assertRaises(core.LicenseError):
            self.manager.install(json.dumps(value).encode())
        self.assertEqual(self.manager.path.read_bytes(), original)
        with self.assertRaises(core.LicenseError):
            self.manager.install(self.module_license(["process_sheet", "process_sheet"]))

    def test_controller_denies_ungranted_or_unregistered_module(self):
        import oa_login_cli as app
        controller = app.AutomationController()
        self.manager.install(self.issue(days=7))
        with patch.object(app, "LICENSES", self.manager):
            with self.assertRaises(core.LicenseError):
                controller.start({"name": "未来测试业务", "module_id": "future_business"})
            with self.assertRaises(ValueError):
                controller.start({"name": "未注册业务"})
        self.assertFalse(controller.task_active)
        self.assertTrue(controller.jobs.empty())

    def test_roundtrip_configurable_days_and_expiry(self):
        for days in (1, 7, 30, 90):
            self.manager.install(self.issue(days=days))
            self.assertEqual(self.manager.require_valid()["remaining_seconds"], days * 86400)
        expiry = datetime.fromtimestamp(self.base + 3600, timezone.utc).isoformat()
        self.manager.install(self.issue(expires_at=expiry))
        self.assertEqual(self.manager.require_valid()["remaining_seconds"], 3600)

    def test_wrong_password_and_invalid_dates(self):
        with self.assertRaises(core.LicenseError):
            issue_license(self.request, self.private, "wrong", "customer", days=1, now=self.base)
        for days in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(core.LicenseError):
                self.issue(days=days)
        with self.assertRaises(core.LicenseError):
            self.issue(expires_at="2020-01-01T00:00:00Z")

    def test_tamper_does_not_replace_existing_license(self):
        valid = self.issue(days=7)
        self.manager.install(valid)
        modified = json.loads(valid)
        modified["payload"]["expires_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(core.LicenseError):
            self.manager.install(json.dumps(modified).encode())
        self.assertEqual(self.manager.path.read_bytes(), valid)
        with self.assertRaises(core.LicenseError):
            core.verify_license(valid, self.keys, "other-device")

    def test_expiry_blocks_new_task_but_does_not_cancel_current(self):
        import oa_login_cli as app
        controller = app.AutomationController()
        controller.worker = Mock()
        controller.worker.is_alive.return_value = True
        self.manager.install(self.issue(days=1))
        with patch.object(app, "LICENSES", self.manager):
            controller.start({"name": "test", "module_id": "process_sheet"})
            self.elapsed = self.wall = 86400
            self.assertFalse(self.manager.status()["valid"])
            self.assertFalse(controller.cancel_event.is_set())
            controller.task_active = False
            with self.assertRaises(core.LicenseError):
                controller.start({"name": "test", "module_id": "process_sheet"})

    def test_daily_check_and_rollback(self):
        self.manager.install(self.issue(days=30))
        self.assertEqual(self.fetch.call_count, 1)
        self.elapsed = self.wall = 500
        for _ in range(10):
            self.assertTrue(self.manager.status()["valid"])
            self.manager.require_valid()
        self.assertEqual(self.fetch.call_count, 1)
        self.wall -= 1000
        self.assertEqual(self.manager.status()["code"], "time_check_required")
        self.manager.require_valid()
        self.assertEqual(self.fetch.call_count, 2)
        self.elapsed += 86401
        self.wall += 86401
        self.fetch.side_effect = core.LicenseError("断网")
        for _ in range(2):
            with self.assertRaises(core.LicenseError):
                self.manager.require_valid()

    def test_restart_requires_online_time(self):
        self.manager.install(self.issue(days=30))
        clock = core.TrustedClock(Mock(side_effect=core.LicenseError("断网")))
        restarted = LicenseManager(lambda: Path(self.temp.name), self.keys, clock, self.device)
        self.assertEqual(restarted.status()["code"], "time_check_required")
        with self.assertRaises(core.LicenseError):
            restarted.require_valid()

    def test_quorum_requires_agreement(self):
        readings = dict(zip(core.TIME_SOURCES, [self.base, self.base + 1, self.base - 10000]))
        with patch.object(core, "_https_timestamp", side_effect=lambda url: readings[url]):
            self.assertEqual(core.fetch_network_time(), self.base + 1)
        readings[core.TIME_SOURCES[1]] += 1000
        with patch.object(core, "_https_timestamp", side_effect=lambda url: readings[url]):
            with self.assertRaises(core.LicenseError):
                core.fetch_network_time()

    def test_unconfigured_and_unlicensed_are_closed(self):
        self.assertFalse(self.manager.status()["valid"])
        with self.assertRaises(core.LicenseError):
            self.manager.require_valid()
        unconfigured = LicenseManager(lambda: Path(self.temp.name), {}, self.clock, self.device)
        with self.assertRaises(core.LicenseError):
            unconfigured.install(self.issue(days=1))
        self.assertFalse(unconfigured.status()["valid"])


if __name__ == "__main__":
    unittest.main()
