"""
Smoke tests — verify that core modules import cleanly, Flask routes are registered,
and unauthed endpoints return the expected 401.

Run:
    venv/bin/python -m pytest tests/
    # or stdlib-only:
    venv/bin/python -m unittest tests.test_smoke
"""
import unittest


class TestImports(unittest.TestCase):
    def test_server_imports(self):
        import server
        self.assertTrue(hasattr(server, "app"))
        self.assertTrue(hasattr(server, "socketio"))

    def test_data_provider_imports(self):
        import data_provider
        self.assertTrue(hasattr(data_provider, "fetch_all"))

    def test_l2_worker_imports(self):
        from background_engine import l2_worker
        self.assertTrue(hasattr(l2_worker, "start_l2_worker"))
        self.assertTrue(hasattr(l2_worker, "get_l2_state"))

    def test_schwab_bridge_imports(self):
        from background_engine import schwab_bridge
        self.assertTrue(hasattr(schwab_bridge, "log"))

    def test_iv_calibrator_imports(self):
        from connectors.iv_calibrator import IVCalibrator
        cal = IVCalibrator(ticker="QQQ", poll_interval=300)
        data = cal.get_calibration()
        self.assertIn("freshness", data)
        self.assertEqual(data["freshness"], "UNINIT")


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.client = server.app.test_client()
        cls.rules = {r.rule for r in server.app.url_map.iter_rules()}

    def test_expected_endpoints_registered(self):
        for ep in ("/api/data", "/api/l2", "/api/chain", "/api/spot",
                   "/api/walls", "/api/vprofile"):
            self.assertIn(ep, self.rules, f"missing route: {ep}")

    def test_unauthed_api_returns_401(self):
        resp = self.client.get("/api/data")
        self.assertIn(resp.status_code, (401, 403),
                      f"expected 401/403, got {resp.status_code}")

    def test_static_root_serves(self):
        resp = self.client.get("/")
        self.assertIn(resp.status_code, (200, 302, 304))


class TestHealthInvariants(unittest.TestCase):
    def test_log_dir_created_on_import(self):
        import server  # noqa: F401
        import os
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        self.assertTrue(os.path.isdir(log_dir), "logs/ dir should be created at import time")

    def test_frameworks_gate_default_off(self):
        import os
        prev = os.environ.pop("ALPHA_ENABLED", None)
        try:
            import importlib
            from background_engine import l2_worker
            importlib.reload(l2_worker)
            self.assertFalse(l2_worker._ALPHA_ENABLED, "frameworks should be off by default")
        finally:
            if prev is not None:
                os.environ["ALPHA_ENABLED"] = prev


if __name__ == "__main__":
    unittest.main()
