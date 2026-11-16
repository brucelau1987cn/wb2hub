"""Contract tests for the maintenance runner; never import gateway modules."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_upstream_tests.py"


class RunnerContractTests(unittest.TestCase):
    def load_runner(self):
        self.assertTrue(SCRIPT.is_file(), "The explicit safe upstream runner is missing")
        spec = importlib.util.spec_from_file_location("safe_upstream_runner", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_empty_and_unknown_allowlists_are_rejected(self):
        runner = self.load_runner()
        for names in ([], ["_test_typo.py"], ["../wb_proxy.py"],
                      ["_test_daily_chat_activity_fallback.py"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                runner.validate_selection(names)

    def test_all_defaults_exist_and_cover_required_surfaces(self):
        runner = self.load_runner()
        selected = runner.validate_selection(list(runner.ALLOWLIST))
        self.assertGreater(len(selected), 0)
        self.assertEqual(len(selected), len(set(selected)))
        for name in selected:
            self.assertTrue((ROOT / "tests" / name).is_file(), name)
        for name in ("_test_health_auth.py", "_test_http_limits.py",
                     "_test_upstream_pool.py", "_test_shared_ssl_context.py",
                     "_test_tls_fingerprint.py", "_test_dashboard_handlers.js"):
            self.assertIn(name, selected)

    def test_environment_does_not_inherit_credentials_or_proxy(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as work, mock.patch.dict(
                os.environ, {"OPENAI_API_KEY": "private", "WB_API_KEY": "private",
                             "HTTP_PROXY": "http://private", "PYTHONPATH": "/private"}):
            env = runner.clean_environment(Path(work))
        for key in ("OPENAI_API_KEY", "WB_API_KEY", "HTTP_PROXY"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PYTHONPATH"], str(ROOT))
        self.assertEqual(env["HOME"], str(Path(work) / "home"))
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_missing_node_and_helper_fail_without_running_tests(self):
        runner = self.load_runner()
        with mock.patch.object(runner.shutil, "which", return_value=None):
            self.assertIn("node", runner.dependency_error("_test_dashboard_handlers.js"))
        with mock.patch.object(runner, "bridge_candidates", return_value=[]):
            self.assertIn("bridge", runner.dependency_error("_test_tls_fingerprint.py"))

    def test_log_summary_timeout_and_start_error(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as work:
            logs = Path(work)
            with mock.patch.object(runner, "dependency_error", return_value=None), \
                    mock.patch.object(runner, "suite_command", return_value=[
                        sys.executable, "-c", "print('complete fixture output')"]):
                passed = runner.run_one("_test_health_auth.py", logs, 5)
            self.assertEqual(passed["status"], "passed")
            self.assertIn("complete fixture output", Path(passed["log"]).read_text())
            with mock.patch.object(runner, "dependency_error", return_value=None), \
                    mock.patch.object(runner, "suite_command", return_value=[
                        sys.executable, "-c", "import time; time.sleep(10)"]):
                expired = runner.run_one("_test_health_auth.py", logs, 0.1)
            self.assertEqual(expired["status"], "timeout")
            with mock.patch.object(runner, "dependency_error", return_value=None), \
                    mock.patch.object(runner, "suite_command", return_value=[
                        str(logs / "missing-interpreter")]):
                broken = runner.run_one("_test_health_auth.py", logs, 5)
            self.assertEqual(broken["status"], "error")
            with mock.patch.object(runner, "run_one", return_value=passed):
                code = runner.main(["--logs", str(logs), "--suites", "_test_health_auth.py"])
            summary = json.loads((logs / "summary.json").read_text())
            self.assertEqual(code, 0)
            self.assertEqual(summary["upstream_total"], 290)
            self.assertEqual(summary["selected_count"], 1)
            self.assertEqual(summary["counts"]["passed"], 1)
            self.assertEqual(len(summary["results"]), 1)

    def test_unittest_skips_are_not_reported_as_full_pass(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as work, \
                mock.patch.object(runner, "dependency_error", return_value=None), \
                mock.patch.object(runner, "suite_command", return_value=[
                    sys.executable, "-c", "print('OK (skipped=2)')"]):
            result = runner.run_one("_test_tls_fingerprint.py", Path(work), 5)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skipped_tests"], 2)

    def test_cli_typo_and_empty_selection_exit_nonzero(self):
        self.load_runner()
        for extra in (["--suites"], ["--suites", "_test_typo.py"], ["--timeout", "0"]):
            done = subprocess.run([sys.executable, str(SCRIPT), *extra], cwd=ROOT,
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
