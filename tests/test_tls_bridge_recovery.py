"""Offline TLS bridge lifecycle regression tests; no upstream requests."""
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_upstream_pool as pool


class Child:
    def __init__(self):
        self.returncode = None
        self.stdin = self.stdout = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class TLSBridgeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "WB_TLS_FINGERPRINT": "auto", "WB_TLS_BRIDGE": "auto",
            "WB_UPSTREAM_KEEPALIVE": "1", "WB_CA_BUNDLE": "",
            "SSL_CERT_FILE": "", "WB_TLS_BRIDGE_SECRET": "",
        })
        self.env.start()
        pool.reset_tls_state()
        self.now = 1000.0
        self.clock = mock.patch.object(pool.time, "monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.binaries = mock.patch.object(pool, "_bridge_binary_candidates", return_value=["fixture"])
        self.binaries.start()
        self.children = []
        self.spawn = mock.patch.object(pool, "_spawn_auto_tls_bridge", side_effect=self.spawn_child)
        self.spawn_mock = self.spawn.start()
        self.logs = []
        pool.set_tls_logger(lambda message, **kw: self.logs.append((message, kw)))

    def tearDown(self):
        pool.close_all()
        pool.reset_tls_state()
        pool.set_tls_logger(None)
        self.spawn.stop()
        self.binaries.stop()
        self.clock.stop()
        self.env.stop()

    def spawn_child(self, _path):
        child = Child()
        self.children.append(child)
        return child, "fixture-secret" * 4, ("127.0.0.1", 41234)

    def route(self):
        return pool._resolve_tls_route("https", "offline.example.test")

    def test_obsolete_startup_checks_cannot_kill_new_configuration(self):
        for mode in ("auto", "node"):
            for reason in ("unsupported_ssl_factory", "unsupported_connection_class"):
                with self.subTest(mode=mode, reason=reason):
                    pool.reset_tls_state()
                    os.environ["WB_TLS_FINGERPRINT"] = mode
                    entered, release = threading.Event(), threading.Event()
                    original = pool._start_bundled_tls_bridge
                    results, errors = [], []

                    def start(*args, **kwargs):
                        try:
                            return original(*args, **kwargs)
                        except pool.TlsBridgeUnavailable as exc:
                            if exc.reason == reason:
                                entered.set()
                                if not release.wait(3):
                                    raise AssertionError("race release timed out")
                            raise

                    def obsolete_request():
                        try:
                            results.append(self.route())
                        except Exception as exc:
                            errors.append(exc)

                    patcher = (mock.patch.object(pool, "_resolve_ssl_factory", return_value=(object(), None))
                               if reason == "unsupported_ssl_factory" else
                               mock.patch.object(pool, "_PooledHTTPSConnection", object()))
                    with mock.patch.object(pool, "_start_bundled_tls_bridge", side_effect=start):
                        patcher.start()
                        thread = threading.Thread(target=obsolete_request)
                        thread.start()
                        try:
                            self.assertTrue(entered.wait(2))
                            patcher.stop()
                            current = self.route()
                            child = self.children[-1]
                        finally:
                            patcher.stop()
                            release.set()
                            thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertIsNone(child.poll(), "obsolete startup check killed new child")
                    self.assertEqual(self.route()["generation"], current["generation"])
                    self.assertEqual(pool.tls_status()["bridge_state"], "ready")
                    if mode == "auto":
                        self.assertEqual(results[0]["route"], "python")
                        self.assertEqual(errors, [])
                    else:
                        self.assertEqual(len(errors), 1)
                        self.assertIsInstance(errors[0], pool.TlsBridgeUnavailable)
                        self.assertEqual(errors[0].reason, reason)

    def test_startup_discovery_exception_is_generation_bound(self):
        for mode in ("auto", "node"):
            for discovery in ("_resolve_ssl_factory", "_bridge_binary_candidates"):
                with self.subTest(mode=mode, discovery=discovery):
                    pool.reset_tls_state()
                    os.environ["WB_TLS_FINGERPRINT"] = mode
                    entered, release = threading.Event(), threading.Event()
                    results, errors = [], []
                    def obsolete_request():
                        try:
                            results.append(self.route())
                        except Exception as exc:
                            errors.append(exc)
                    # For SSL discovery, fail only startup, not config-key discovery.
                    original = getattr(pool, discovery)
                    # No lock-blocking race is needed: classify first, delay the
                    # error handler, then install a new configuration.
                    original_start = pool._start_bundled_tls_bridge
                    def start(*args, **kwargs):
                        try:
                            return original_start(*args, **kwargs)
                        except Exception:
                            if threading.current_thread() is worker:
                                entered.set()
                                if not release.wait(3):
                                    raise AssertionError("race release timed out")
                            raise
                    # Discovery itself must raise immediately while startup owns
                    # the lock; pause only after that lock has unwound.
                    calls = 0
                    def broken_discovery():
                        nonlocal calls
                        if threading.current_thread() is worker:
                            calls += 1
                            if discovery != "_resolve_ssl_factory" or calls > 1:
                                raise RuntimeError("fixture discovery failed")
                        return original()
                    worker = threading.Thread(target=obsolete_request)
                    with mock.patch.object(pool, discovery, side_effect=broken_discovery), \
                            mock.patch.object(pool, "_start_bundled_tls_bridge", side_effect=start):
                        worker.start()
                        try:
                            self.assertTrue(entered.wait(2))
                            os.environ["WB_TLS_BRIDGE_SECRET"] = mode + discovery
                            current = self.route()
                            child = self.children[-1]
                        finally:
                            release.set()
                            worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertIsNone(child.poll())
                    self.assertEqual(self.route()["transport"], current["transport"])
                    if mode == "auto":
                        self.assertEqual(errors, [])
                        self.assertEqual(results[0]["reason"], "start_failed")
                    else:
                        self.assertEqual(len(errors), 1)
                        self.assertIsInstance(errors[0], pool.TlsBridgeUnavailable)
                        self.assertEqual(errors[0].reason, "start_failed")

    def test_socket_timeout_has_same_safe_category_on_python39(self):
        error = pool.TlsBridgeConnectError("bridge_local_io", error=socket.timeout("SECRET"))
        self.assertEqual(error.error_type, "TimeoutError")
        self.assertNotIn("SECRET", str(error))

    def test_exit_diagnostics_wait_for_output_drain_before_snapshot(self):
        self.route()
        child = self.children[-1]
        child.returncode = 2
        def stop(proc):
            proc._wb_diagnostics = {"stderr_category": "panic", "stderr_bytes": 200,
                                    "stdout_bytes": 0}
        with mock.patch.object(pool, "_stop_tls_bridge_process", side_effect=stop):
            self.route()
        self.assertEqual(pool.tls_status()["last_failure"]["stderr_category"], "panic")

    def test_unusable_executable_is_permanent_configuration_failure(self):
        self.spawn_mock.side_effect = OSError(8, "Exec format error SECRET")
        self.assertEqual(self.route()["route"], "python")
        self.assertFalse(pool.tls_status()["auto_retry_available"])
        self.assertEqual(pool.tls_status()["last_fallback_reason"], "binary_unusable")
        self.assertNotIn("SECRET", str(self.logs))

    def test_external_snapshot_cannot_pair_old_address_with_new_generation(self):
        for mode in ("auto", "node"):
            with self.subTest(mode=mode):
                pool.reset_tls_state()
                os.environ["WB_TLS_FINGERPRINT"] = mode
                os.environ["WB_TLS_BRIDGE"] = "127.0.0.1:40001"
                os.environ["WB_TLS_BRIDGE_SECRET"] = "old-secret"
                entered, release = threading.Event(), threading.Event()
                results, errors = [], []
                original = pool._external_tls_bridge
                def external():
                    snapshot = original()
                    if threading.current_thread() is worker:
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("race release timed out")
                    return snapshot
                def obsolete_request():
                    try:
                        results.append(self.route())
                    except Exception as exc:
                        errors.append(exc)
                worker = threading.Thread(target=obsolete_request)
                with mock.patch.object(pool, "_external_tls_bridge", side_effect=external):
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        os.environ["WB_TLS_BRIDGE"] = "127.0.0.1:40002"
                        os.environ["WB_TLS_BRIDGE_SECRET"] = "new-secret"
                        current = self.route()
                    finally:
                        release.set()
                        worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(self.route()["transport"], current["transport"])
                if mode == "auto":
                    self.assertEqual(results[0]["route"], "python", results)
                    self.assertEqual(results[0]["reason"], "start_superseded")
                    self.assertEqual(errors, [])
                else:
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], pool.TlsBridgeUnavailable)
                    self.assertEqual(errors[0].reason, "start_superseded")

    def test_obsolete_external_validation_error_cannot_kill_new_child(self):
        os.environ["WB_TLS_BRIDGE"] = "127.0.0.1:40001"
        os.environ["WB_TLS_BRIDGE_SECRET"] = ""
        entered, release = threading.Event(), threading.Event()
        original = pool._external_tls_bridge
        results = []
        def external():
            try:
                return original()
            except ValueError:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("race release timed out")
                raise
        with mock.patch.object(pool, "_external_tls_bridge", side_effect=external):
            worker = threading.Thread(target=lambda: results.append(self.route()))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                os.environ["WB_TLS_BRIDGE"] = "auto"
                current = self.route()
                child = self.children[-1]
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["route"], "python")
        self.assertIsNone(child.poll(), "obsolete external validation killed current child")
        self.assertEqual(self.route()["transport"], current["transport"])
        self.assertEqual(pool.tls_status()["bridge_state"], "ready")

    def test_external_validation_error_is_bound_to_generation_even_after_config_returns(self):
        os.environ["WB_TLS_BRIDGE"] = "127.0.0.1:40001"
        os.environ["WB_TLS_BRIDGE_SECRET"] = "external-secret"
        entered, release = threading.Event(), threading.Event()
        original = pool._external_tls_bridge
        results = []
        def external():
            if threading.current_thread() is worker:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("race release timed out")
                # A validation failure from the obsolete configuration epoch.
                raise ValueError("fixture invalid external setting")
            return original()
        worker = threading.Thread(target=lambda: results.append(self.route()))
        with mock.patch.object(pool, "_external_tls_bridge", side_effect=external):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                os.environ["WB_TLS_BRIDGE"] = "auto"
                self.route()
                os.environ["WB_TLS_BRIDGE"] = "127.0.0.1:40001"
                current = self.route()
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["route"], "python")
        self.assertEqual(pool.tls_status()["bridge_state"], "external")
        self.assertEqual(self.route()["transport"], current["transport"])

    def test_strict_external_bridge_failure_never_selects_python(self):
        os.environ["WB_TLS_FINGERPRINT"] = "node"
        os.environ["WB_TLS_BRIDGE"] = "127.0.0.1:40000"
        os.environ["WB_TLS_BRIDGE_SECRET"] = "fixture" * 8
        pool._sync_tls_state()
        pool._mark_bridge_unavailable("external_unavailable")
        with self.assertRaises(pool.TlsBridgeUnavailable):
            self.route()

    def test_retry_start_failures_consume_budget_and_config_change_resets_it(self):
        self.spawn_mock.side_effect = pool.TlsBridgeUnavailable("start_exited")
        self.assertEqual(self.route()["reason"], "start_exited")
        for used, delay in enumerate((30, 120, 600), 1):
            self.now += delay
            self.assertEqual(self.route()["route"], "python")
            self.assertEqual(pool.tls_status()["retry_attempts_used"], used)
        self.assertTrue(pool.tls_status()["retry_exhausted"])
        self.assertEqual(self.spawn_mock.call_count, 4)
        os.environ["WB_TLS_BRIDGE_SECRET"] = "new-epoch"
        self.spawn_mock.side_effect = self.spawn_child
        self.assertEqual(self.route()["route"], "bridge")
        self.assertEqual(pool.tls_status()["retry_attempts_used"], 0)

    def test_permanent_errors_are_never_retried(self):
        for reason in ("binary_missing", "bridge_auth_failed", "bridge_auth_unavailable",
                       "external_invalid", "unsupported_ssl_factory", "bridge_protocol_error"):
            with self.subTest(reason=reason):
                pool.reset_tls_state()
                self.route()
                pool._mark_bridge_unavailable(reason)
                count = self.spawn_mock.call_count
                self.now += 100000
                self.assertEqual(self.route()["route"], "python")
                self.assertEqual(self.spawn_mock.call_count, count)
                self.assertFalse(pool.tls_status()["auto_retry_available"])
                self.assertFalse(pool.tls_status()["retry_exhausted"])

    def test_external_python_and_strict_node_never_enter_recovery(self):
        self.route()
        for mode, setting in (("auto", "127.0.0.1:40000"), ("python", "auto"), ("node", "auto")):
            with self.subTest(mode=mode, setting=setting):
                pool.reset_tls_state()
                os.environ["WB_TLS_FINGERPRINT"] = mode
                os.environ["WB_TLS_BRIDGE"] = setting
                os.environ["WB_TLS_BRIDGE_SECRET"] = "fixture" * 8
                pool._sync_tls_state()
                pool._mark_bridge_unavailable("runtime_exited")
                self.now += 100000
                before = self.spawn_mock.call_count
                if mode == "node":
                    with self.assertRaises(pool.TlsBridgeUnavailable):
                        self.route()
                else:
                    self.assertEqual(self.route()["route"], "python")
                self.assertFalse(pool.tls_status()["auto_retry_available"])
                self.assertEqual(self.spawn_mock.call_count, before)

    def test_concurrent_retry_only_spawns_once_and_drops_old_idle_connections(self):
        old = self.route()
        idle = mock.Mock()
        key = ("https", "offline.example.test", 443, "", "", old["transport"])
        pool._POOL.release(key, idle)
        self.children[-1].returncode = 1
        self.route()
        idle.close.assert_called_once()
        self.now += 30
        entered, release = threading.Event(), threading.Event()
        def spawn(path):
            entered.set()
            release.wait(3)
            return self.spawn_child(path)
        self.spawn_mock.side_effect = spawn
        results = []
        leader = threading.Thread(target=lambda: results.append(self.route()))
        leader.start()
        try:
            self.assertTrue(entered.wait(2))
            workers = [threading.Thread(target=lambda: results.append(self.route())) for _ in range(20)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(2)
            self.assertEqual(pool.tls_status()["bridge_state"], "starting")
        finally:
            release.set()
            leader.join(3)
        self.assertEqual(self.spawn_mock.call_count, 2)
        self.assertEqual(sum(item["route"] == "bridge" for item in results), 1)
        self.assertEqual(pool.tls_status()["retry_attempts_used"], 1)
        self.assertFalse(pool._transport_is_current(old["transport"]))

    def test_real_bundled_child_kill_recovers_and_connects_only_to_loopback(self):
        candidates = [path for path in pool._tls_bridge_candidates()
                      if os.path.isfile(path) and os.access(path, os.X_OK)]
        self.assertTrue(candidates, "real bundled helper required; do not skip")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(3)
        captured = []
        errors = []
        def fixture():
            try:
                for _ in range(2):
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(3)
                        captured.append(conn.recv(4096))
            except Exception as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=fixture)
        thread.start()
        self.spawn.stop()
        with mock.patch.object(pool, "_bridge_binary_candidates", return_value=candidates[:1]):
            try:
                old = self.route()
                for route in (old, None):
                    if route is None:
                        pool._TLS_BRIDGE_PROCESS.kill()
                        pool._TLS_BRIDGE_PROCESS.wait(timeout=3)
                        self.assertEqual(self.route()["route"], "python")
                        self.assertEqual(pool.tls_status()["last_failure"]["returncode"], -9)
                        self.now += 30
                        route = self.route()
                        self.assertNotEqual(route["generation"], old["generation"])
                    self.assertEqual(route["route"], "bridge")
                    conn = pool._new_connection("https", "offline.example.test", 443, None, 3, route=route)
                    with self.assertRaises(pool.TlsBridgeConnectError) as caught:
                        conn._connect_via_tls_bridge(route, "127.0.0.1", listener.getsockname()[1])
                    self.assertEqual(caught.exception.reason, "upstream_tls_or_connect_failed")
                    self.assertFalse(caught.exception.bridge_unavailable)
                self.assertEqual(pool.tls_status()["bridge_state"], "ready")
            finally:
                listener.close()
                thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(data.startswith(b"\x16\x03") for data in captured))

    def test_retry_availability_respects_disabled_pool_and_custom_ca(self):
        self.assertTrue(pool.tls_status()["auto_retry_available"])
        os.environ["WB_UPSTREAM_KEEPALIVE"] = "0"
        self.assertFalse(pool.tls_status()["auto_retry_available"])
        os.environ["WB_UPSTREAM_KEEPALIVE"] = "1"
        os.environ["WB_CA_BUNDLE"] = __file__
        self.assertFalse(pool.tls_status()["auto_retry_available"])

    def test_route_never_pairs_old_address_with_new_generation_secret(self):
        entered, release = threading.Event(), threading.Event()
        original = pool._start_bundled_tls_bridge
        results = []
        def start(mode, **kwargs):
            address = original(mode, **kwargs)
            if not entered.is_set():
                entered.set()
                release.wait(3)
            return address
        with mock.patch.object(pool, "_start_bundled_tls_bridge", side_effect=start):
            thread = threading.Thread(target=lambda: results.append(self.route()))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                os.environ["WB_TLS_BRIDGE_SECRET"] = "new-config"
                self.spawn_mock.side_effect = lambda _: (Child(), "new-secret", ("127.0.0.1", 40002))
                self.route()
            finally:
                release.set()
                thread.join(3)
        self.assertEqual(results[0]["route"], "python", results)
        self.assertEqual(pool.tls_status()["bridge_state"], "ready")

    def test_local_eof_has_explicit_diagnostic_distinct_from_timeout(self):
        route = self.route()
        sock = mock.Mock()
        sock.recv.return_value = b""
        conn = pool._new_connection("https", "offline.example.test", 443, None, 1, route=route)
        with mock.patch.object(pool.socket, "create_connection", return_value=sock):
            with self.assertRaises(pool.TlsBridgeConnectError) as caught:
                conn.connect()
        self.assertEqual(caught.exception.error_type, "EOFError")
        pool._fallback_after_bridge_connect(route, caught.exception, "https", "offline.example.test", None)
        self.assertEqual(pool.tls_status()["last_failure"]["error_type"], "EOFError")

    def test_thread_start_failure_reaps_child_and_closes_all_pipes(self):
        self.spawn.stop()
        real_popen = subprocess.Popen
        real_start = threading.Thread.start
        for failure_at in (1, 2, 3):  # stderr drain, LISTEN reader, stdout drain
            with self.subTest(thread=failure_at):
                launched = []
                count = 0
                def launch(*_args, **kwargs):
                    proc = real_popen([sys.executable, "-u", "-c",
                        "import sys,time;sys.stdin.readline();"
                        "print('LISTEN 127.0.0.1:45678',flush=True);time.sleep(30)"], **kwargs)
                    launched.append(proc)
                    return proc
                def start(thread):
                    nonlocal count
                    count += 1
                    if count == failure_at:
                        raise RuntimeError("fixture thread allocation failed")
                    return real_start(thread)
                try:
                    with mock.patch.object(pool.subprocess, "Popen", side_effect=launch), \
                            mock.patch.object(threading.Thread, "start", new=start):
                        with self.assertRaises(RuntimeError) as caught:
                            pool._spawn_auto_tls_bridge("fixture")
                    proc = launched[0]
                    self.assertIsNotNone(proc.poll(), "thread failure leaked live bridge child")
                    self.assertTrue(all(stream.closed for stream in (proc.stdin, proc.stdout, proc.stderr)))
                    self.assertEqual(str(caught.exception), "fixture thread allocation failed")
                finally:
                    for proc in launched:
                        proc.kill()
                        proc.wait(timeout=3)
                        for thread in getattr(proc, "_wb_drain_threads", ()):
                            if thread.ident is not None:
                                thread.join(1)
                        for stream in (proc.stdin, proc.stdout, proc.stderr):
                            stream.close()

    def test_start_timeout_preserves_pre_kill_alive_and_safe_stderr(self):
        real_popen = subprocess.Popen
        script = "import sys,time;sys.stdin.readline();sys.stderr.write('panic: secret-body\\n');sys.stderr.flush();time.sleep(30)"
        def launch(*_args, **kwargs):
            return real_popen([sys.executable, "-u", "-c", script], **kwargs)
        self.spawn.stop()
        with mock.patch.object(pool.subprocess, "Popen", side_effect=launch), \
                mock.patch.object(pool, "_TLS_BRIDGE_START_TIMEOUT", 0.1):
            self.assertEqual(self.route()["route"], "python")
        failure = pool.tls_status()["last_failure"]
        self.assertEqual(failure["child_state"], "alive")
        self.assertIsNone(failure["returncode"])
        self.assertEqual(failure["stderr_category"], "panic")
        self.assertTrue(pool.tls_status()["auto_retry_available"])
        self.assertNotIn("secret-body", str(failure) + str(self.logs))

    def test_runtime_exit_preserves_safe_child_output_diagnostics(self):
        self.route()
        self.children[-1]._wb_diagnostics = {"stdout_bytes": 10, "stderr_bytes": 400,
                                              "stderr_category": "panic"}
        self.children[-1].returncode = 2
        self.route()
        failure = pool.tls_status()["last_failure"]
        self.assertEqual(failure.get("stderr_category"), "panic")
        self.assertEqual(failure.get("stderr_bytes"), 400)
        self.assertEqual(failure["returncode"], 2)
        self.assertEqual(failure["child_state"], "exited")
        self.assertIn("stderr=panic", str(self.logs))

    def test_child_output_is_drained_but_only_safe_diagnostic_categories_survive(self):
        real_popen = subprocess.Popen
        script = (
            "import sys,time\n"
            "secret=sys.stdin.readline().strip()\n"
            "sys.stderr.write('panic: RAW-TOKEN '+secret+' CONNECT header upstream body\\n'+'x'*200000)\n"
            "sys.stderr.flush()\n"
            "print('LISTEN 127.0.0.1:45678',flush=True)\n"
            "sys.stdout.write('x'*200000);sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        def launch(*_args, **kwargs):
            return real_popen([sys.executable, "-u", "-c", script], **kwargs)
        # Use the actual spawn implementation, not the lifecycle fixture.
        self.spawn.stop()
        with mock.patch.object(pool.subprocess, "Popen", side_effect=launch):
            proc, secret, address = pool._spawn_auto_tls_bridge("fixture")
        try:
            self.assertEqual(address, ("127.0.0.1", 45678))
            self.assertTrue(hasattr(proc, "_wb_diagnostics"), "stderr was discarded")
            done = threading.Event()
            deadline = time.perf_counter() + 2
            while proc._wb_diagnostics.get("stdout_bytes", 0) < 200000 and time.perf_counter() < deadline:
                done.wait(0.01)
            diag = proc._wb_diagnostics
            self.assertGreaterEqual(diag["stderr_bytes"], 200000)
            self.assertGreaterEqual(diag["stdout_bytes"], 200000)
            self.assertEqual(diag["stderr_category"], "panic")
            self.assertNotIn("RAW-TOKEN", str(diag))
            self.assertNotIn(secret, str(diag))
        finally:
            pool._stop_tls_bridge_process(proc)

    def test_bridge_redirect_post_is_not_replayed_by_account_wrappers(self):
        # Execute the real outer wrappers without importing account storage.
        import ast
        import io
        import json
        import ssl
        from pathlib import Path
        source = Path(pool.__file__).with_name("wb_accounts.py")
        tree = ast.parse(source.read_text())
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name in ("urlopen", "http_json", "_retryable")]
        ns = {"wb_upstream_pool": pool, "urllib": __import__("urllib"),
              "json": json, "ssl": ssl, "time": time,
              "opener_for_proxy": lambda _: None,
              "is_tls_verify_error": lambda _: False}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), ns)
        for mode in ("auto", "node"):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(mode=mode, code=code):
                    os.environ["WB_TLS_FINGERPRINT"] = mode
                    route = self.route()
                    listener = socket.socket()
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(1)
                    listener.settimeout(3)
                    route["bridge_address"] = listener.getsockname()
                    requests, errors, responses = [], [], []
                    body = b'{"redirect":"not success"}'

                    def fixture():
                        try:
                            conn, _ = listener.accept()
                            with conn:
                                conn.settimeout(3)
                                def headers():
                                    data = b""
                                    while not data.endswith(b"\r\n\r\n"):
                                        chunk = conn.recv(1)
                                        if not chunk:
                                            raise EOFError("fixture HTTP headers incomplete")
                                        data += chunk
                                    return data
                                headers()  # local CONNECT, not an upstream request
                                conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                                request = headers()
                                length = next(int(line.split(b":", 1)[1])
                                              for line in request.split(b"\r\n")
                                              if line.lower().startswith(b"content-length:"))
                                data = b""
                                while len(data) < length:
                                    chunk = conn.recv(length - len(data))
                                    if not chunk:
                                        raise EOFError("fixture POST incomplete")
                                    data += chunk
                                requests.append((request, data))
                                conn.sendall(("HTTP/1.1 %d Redirect\r\nLocation: /other\r\n"
                                              "Content-Length: %d\r\nConnection: close\r\n\r\n"
                                              % (code, len(body))).encode() + body)
                        except Exception as exc:
                            errors.append(exc)

                    original_read = pool._read_error_body
                    def read_error(resp):
                        responses.append(resp)
                        return original_read(resp)
                    thread = threading.Thread(target=fixture)
                    thread.start()
                    replay_response = io.BytesIO(b'{"ok":true}')
                    try:
                        with mock.patch.object(pool, "_resolve_tls_route", return_value=route), \
                                mock.patch.object(pool, "_read_error_body", side_effect=read_error), \
                                mock.patch.object(urllib.request, "urlopen", return_value=replay_response) as replay:
                            with self.assertRaises(urllib.error.HTTPError) as caught:
                                ns["http_json"]("https://offline.example.test/write", data=b"one-write", retries=3)
                            error = caught.exception
                            try:
                                self.assertEqual(error.code, code)
                                self.assertEqual(error.headers["Location"], "/other")
                                self.assertEqual(error.read(), body)
                            finally:
                                error.close()
                            self.assertTrue(error.fp.closed)
                            replay.assert_not_called()
                    finally:
                        listener.close()
                        replay_response.close()
                        thread.join(4)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(len(requests), 1)
                    self.assertTrue(requests[0][0].startswith(b"POST /write "))
                    self.assertEqual(requests[0][1], b"one-write")
                    self.assertTrue(responses[0].isclosed())
                    self.assertTrue(responses[0]._released)

    def test_sent_bridge_request_is_not_replayed_after_disconnect(self):
        route = self.route()
        req = urllib.request.Request("https://offline.example.test/", data=b"one-write")
        conn = mock.Mock()
        conn.request.return_value = None
        conn.getresponse.side_effect = ConnectionResetError("already processed")
        with mock.patch.object(pool._POOL, "acquire", return_value=(conn, True)), \
                mock.patch.object(pool, "_new_connection", side_effect=AssertionError("sent HTTP replayed")) as new:
            with self.assertRaises(ConnectionResetError):
                pool.urlopen(req)
            self.assertEqual(conn.request.call_count, 1)
            new.assert_not_called()
        self.assertEqual(self.route()["generation"], route["generation"])

    def test_config_change_discards_inflight_spawn_without_killing_new_child(self):
        entered, release = threading.Event(), threading.Event()
        old_child, new_child = Child(), Child()
        results = []
        def spawn(_path):
            if not entered.is_set():
                entered.set()
                release.wait(3)
                return old_child, "old" * 32, ("127.0.0.1", 40001)
            return new_child, "new" * 32, ("127.0.0.1", 40002)
        self.spawn_mock.side_effect = spawn
        thread = threading.Thread(target=lambda: results.append(self.route()))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            os.environ["WB_TLS_BRIDGE_SECRET"] = "config-change"
            current = self.route()
            self.assertEqual(current["bridge_address"], ("127.0.0.1", 40002))
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(old_child.poll(), -9, "obsolete startup must be reaped")
        self.assertIsNone(new_child.poll())
        self.assertEqual(self.route()["bridge_address"], current["bridge_address"])
        self.assertEqual(results[0]["route"], "python")

    def test_local_timeout_records_alive_before_parent_kill_without_secrets(self):
        route = self.route()
        child = self.children[-1]
        sock = mock.Mock()
        sock.recv.side_effect = TimeoutError(110, "RAW-TOKEN CONNECT header upstream body")
        conn = pool._new_connection("https", "offline.example.test", 443, None, 1, route=route)
        with mock.patch.object(pool.socket, "create_connection", return_value=sock):
            with self.assertRaises(pool.TlsBridgeConnectError) as caught:
                conn.connect()
        self.assertEqual(getattr(caught.exception, "error_type", None), "TimeoutError")
        self.assertEqual(getattr(caught.exception, "error_errno", None), 110)
        self.assertNotIn("RAW-TOKEN", "".join(traceback.format_exception(
            type(caught.exception), caught.exception, caught.exception.__traceback__)))
        pool._fallback_after_bridge_connect(route, caught.exception, "https", "offline.example.test", None)
        self.assertEqual(child.poll(), -9)
        failure = pool.tls_status().get("last_failure", {})
        self.assertEqual(failure.get("child_state"), "alive", failure)
        self.assertIsNone(failure.get("returncode"))
        self.assertEqual(failure.get("error_type"), "TimeoutError")
        self.assertEqual(failure.get("error_errno"), 110)
        self.assertNotIn("RAW-TOKEN", str(self.logs) + str(pool.tls_status()))
        self.assertNotIn(route["bridge_secret"], str(self.logs) + str(pool.tls_status()))
        self.assertIn("child=alive", str(self.logs))

    def test_old_generation_connect_error_cannot_kill_recovered_child(self):
        old = self.route()
        self.children[-1].returncode = 1
        self.route()
        self.now += 30
        current = self.route()
        child = self.children[-1]
        fallback = pool._fallback_after_bridge_connect(
            old, pool.TlsBridgeConnectError("bridge_local_io"),
            "https", "offline.example.test", None)
        self.assertEqual(fallback["route"], "python")
        self.assertIsNone(child.poll(), "old CONNECT error killed the new bridge")
        self.assertEqual(self.route()["generation"], current["generation"])
        self.assertEqual(pool.tls_status()["retry_attempts_used"], 1)

    def test_runtime_exit_recovers_with_finite_budget_even_after_success(self):
        self.assertEqual(self.route()["route"], "bridge")
        for used, delay in enumerate((30, 120, 600)):
            self.children[-1].returncode = 7
            self.assertEqual(self.route()["route"], "python")
            status = pool.tls_status()
            self.assertTrue(status["auto_retry_available"], status)
            self.assertEqual(status.get("retry_attempts_used"), used)
            self.assertEqual(status.get("retry_attempts_remaining"), 3 - used)
            self.assertEqual(status.get("retry_wait_seconds"), delay)
            for _ in range(5):
                self.assertEqual(self.route()["route"], "python")
            self.now += delay - 0.01
            self.assertEqual(self.route()["route"], "python")
            self.now += 0.01
            self.assertEqual(self.route()["route"], "bridge")
            self.assertEqual(pool.tls_status()["retry_attempts_used"], used + 1)
        self.children[-1].returncode = 7
        self.assertEqual(self.route()["route"], "python")
        self.now += 100000
        self.assertEqual(self.route()["route"], "python")
        status = pool.tls_status()
        self.assertTrue(status["retry_exhausted"])
        self.assertFalse(status["auto_retry_available"])
        self.assertEqual(status["retry_attempts_remaining"], 0)
        self.assertEqual(self.spawn_mock.call_count, 4)
        warnings = [message for message, kw in self.logs if kw.get("level") == "WARN"]
        self.assertEqual(len(warnings), 4, warnings)
        self.assertEqual(sum("recovered" in message for message, _ in self.logs), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
