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


class TestGlobalDeclarations(unittest.TestCase):
    """
    Catch the class of bug where a module-level singleton is assigned inside a
    function without `global`, silently creating a local instead of updating the
    module global.

    This is the bug that caused IVCalibrator + MMTracker data to be dropped from
    every zone_update event — IVCalibrator itself ran fine, but the publish path
    saw module-level _iv_calibrator as None forever.
    """

    # (file, function_name, required_globals): every module-level var that must
    # be assignable inside the named function.
    CASES = [
        (
            "background_engine/schwab_bridge.py",
            "_run_bridge",
            {"_iv_calibrator", "_mm_tracker", "_flow_classifier", "_edge_detector",
             "_dte0_squeeze", "_greek_surface", "_vol_surface", "_streamer"},
        ),
    ]

    def _parse_function(self, file_path, func_name):
        import ast
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, file_path)) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return node
        return None

    def test_globals_declared_where_assigned(self):
        import ast
        for file_path, func_name, required in self.CASES:
            fn = self._parse_function(file_path, func_name)
            self.assertIsNotNone(fn, f"{file_path}::{func_name} not found")

            assigned = set()
            globaled = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            assigned.add(t.id)
                elif isinstance(node, ast.Global):
                    globaled.update(node.names)

            missing = (assigned & required) - globaled
            self.assertFalse(
                missing,
                f"{file_path}::{func_name} assigns {missing} without `global` "
                f"— this silently creates locals and drops the data from consumers. "
                f"Add `global {', '.join(sorted(missing))}` before the assignment."
            )


class TestZoneUpdateContract(unittest.TestCase):
    """Assert zone_update emit has the IV merge block in place."""

    def test_zone_update_merges_iv_calibration(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "background_engine/schwab_bridge.py")) as f:
            src = f.read()
        # Presence check on the inject block. If someone rips this out, test fails.
        for key in ("orats_mid_iv", "orats_smv_vol", "skew_25d", "mm_uncertainty"):
            self.assertIn(
                f"zone_data['{key}']", src,
                f"zone_update emit no longer sets {key!r} — "
                f"audit P1 surfaced this field; do not drop it silently"
            )


if __name__ == "__main__":
    unittest.main()
