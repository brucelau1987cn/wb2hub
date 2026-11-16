#!/usr/bin/env python3
"""Run the reviewed offline baseline, not all archived upstream suites.

Use a disposable, credential-free container with --network none. This runner
sanitizes child environments; it is not a network or filesystem sandbox.
"""
import argparse
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
UPSTREAM_COMMIT = "0230cf9"
UPSTREAM_TOTAL = 290
# Exact file names only. Changes require side-effect review and documentation.
ALLOWLIST = (
    "_test_health_auth.py",
    "_test_panel_route_auth.py",
    "_test_http_limits.py",
    "_test_upstream_pool.py",
    "_test_shared_ssl_context.py",
    "_test_tls_fingerprint.py",
    "_test_dashboard_handlers.js",
    "_test_dashboard_theme.js",
    "_test_dashboard_refresh.js",
)
STATUSES = ("passed", "failed", "error", "timeout", "skipped", "interrupted")


def validate_selection(names):
    if not names:
        raise ValueError("allowlist selection must not be empty")
    unknown = sorted(set(names) - set(ALLOWLIST))
    if unknown:
        raise ValueError("unknown or non-allowlisted suites: " + ", ".join(unknown))
    if len(names) != len(set(names)):
        raise ValueError("duplicate suite names are not allowed")
    return list(names)


def bridge_candidates():
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    system = platform.system().lower()
    suffix = ".exe" if os.name == "nt" else ""
    return [p for p in (
        ROOT / "bin" / ("wb-tls-bridge-%s-%s%s" % (system, arch, suffix)),
        ROOT / "bin" / ("wb-tls-bridge" + suffix),
        ROOT / "tools" / "wb-tls-bridge" / ("wb-tls-bridge" + suffix),
    ) if p.is_file() and os.access(p, os.X_OK)]


def dependency_error(name):
    if not (TESTS / name).is_file():
        return "missing suite: " + name
    helpers = {
        "_test_panel_route_auth.py": ("_isolated_dirs.py",),
        "_test_upstream_pool.py": ("_isolated_dirs.py",),
        "_test_shared_ssl_context.py": ("_isolated_dirs.py",),
    }
    for helper in helpers.get(name, ()):
        if not (TESTS / helper).is_file():
            return "missing helper: " + helper
    if name.endswith(".js") and shutil.which("node") is None:
        return "node is missing from PATH; dashboard baseline cannot run"
    if name == "_test_tls_fingerprint.py" and not bridge_candidates():
        return "executable wb-tls-bridge helper is missing; full TLS baseline cannot run"
    return None


def clean_environment(work):
    # Do not inherit WB_*, API keys, proxy settings, Python hooks or real HOME.
    env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"
    ) if key in os.environ}
    for name in ("home", "accounts", "usage", "tmp", "config", "cache", "data"):
        (work / name).mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(work / "home"), "USERPROFILE": str(work / "home"),
        "XDG_CONFIG_HOME": str(work / "config"),
        "XDG_CACHE_HOME": str(work / "cache"), "XDG_DATA_HOME": str(work / "data"),
        "ACCOUNTS_DIR": str(work / "accounts"),
        "WB_PROXY_USAGE_DIR": str(work / "usage"),
        "TMPDIR": str(work / "tmp"), "TEMP": str(work / "tmp"),
        "TMP": str(work / "tmp"),
        "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "*", "no_proxy": "*", "LANG": "C.UTF-8",
        # Pool/CA tests must exercise stdlib TLS rather than auto-spawn a bridge.
        # The fingerprint suite explicitly patches modes where needed.
        "WB_TLS_FINGERPRINT": "python",
    })
    return env


def suite_command(name):
    return (["node"] if name.endswith(".js") else [sys.executable]) + [str(TESTS / name)]


def stop_process_group(proc):
    if os.name == "nt":
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def run_one(name, logs, timeout):
    started = time.monotonic()
    path = logs / (name + ".log")
    result = {"name": name, "status": "error", "returncode": None,
              "seconds": 0.0, "log": str(path), "error": None, "skipped_tests": 0}
    proc = None
    try:
        with path.open("w", encoding="utf-8") as sink:
            missing = dependency_error(name)
            if missing:
                result["error"] = missing
                sink.write(missing + "\n")
            else:
                with tempfile.TemporaryDirectory(prefix="wb-upstream-") as directory:
                    try:
                        proc = subprocess.Popen(
                            suite_command(name), cwd=ROOT,
                            env=clean_environment(Path(directory)),
                            stdout=sink, stderr=subprocess.STDOUT,
                            start_new_session=os.name != "nt",
                            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                                           if os.name == "nt" else 0))
                        result["returncode"] = proc.wait(timeout=timeout)
                        result["status"] = "passed" if proc.returncode == 0 else "failed"
                    except subprocess.TimeoutExpired:
                        result.update(status="timeout", error="suite exceeded %.3fs" % timeout)
                    except KeyboardInterrupt:
                        result.update(status="interrupted", error="runner interrupted")
                    finally:
                        if proc is not None:
                            stop_process_group(proc)
                    if result["error"]:
                        sink.write("\nRUNNER: " + result["error"] + "\n")
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = re.findall(r"\bOK \(skipped=(\d+)\)", text)
        result["skipped_tests"] = sum(int(count) for count in matches)
        if result["status"] == "passed" and result["skipped_tests"]:
            result.update(status="skipped", error="suite contains skipped tests; baseline incomplete")
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(status="error", error="%s: %s" % (type(exc).__name__, exc))
        try:
            with path.open("a", encoding="utf-8") as sink:
                sink.write("\nRUNNER: " + result["error"] + "\n")
        except OSError:
            pass
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def write_summary(logs, names, results):
    counts = {status: sum(item["status"] == status for item in results) for status in STATUSES}
    summary = {
        "schema_version": 1, "upstream_commit": UPSTREAM_COMMIT,
        "upstream_total": UPSTREAM_TOTAL, "allowlist_count": len(ALLOWLIST),
        "selected_count": len(names), "selected": names, "completed_count": len(results),
        "complete": len(results) == len(names),
        "ok": len(results) == len(names) and counts["passed"] == len(names),
        "counts": counts, "results": results,
    }
    staging = logs / "summary.json.new"
    staging.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    staging.replace(logs / "summary.json")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, default=None, help="full logs and summary.json directory")
    parser.add_argument("--suites", nargs="*", default=None, help="exact allowlisted filenames only")
    parser.add_argument("--timeout", type=float, default=180, help="per-suite seconds (default: 180)")
    parser.add_argument("--list", action="store_true", help="list reviewed suites without running them")
    args = parser.parse_args(argv)
    try:
        names = validate_selection(list(ALLOWLIST) if args.suites is None else args.suites)
    except ValueError as exc:
        parser.error(str(exc))
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if args.list:
        print("\n".join(names))
        return 0
    results = []
    try:
        logs = args.logs.resolve() if args.logs else Path(tempfile.mkdtemp(prefix="wb-upstream-logs-"))
        logs.mkdir(parents=True, exist_ok=True)
        print("Full logs and JSON summary: " + str(logs), flush=True)
        write_summary(logs, names, results)
        with (logs / "run.log").open("w", encoding="utf-8") as combined:
            for name in names:
                item = run_one(name, logs, args.timeout)
                results.append(item)
                heading = "[%s] %s (%.3fs)" % (item["status"].upper(), name, item["seconds"])
                print(heading + (": " + item["error"] if item["error"] else ""), flush=True)
                combined.write(heading + "\n")
                combined.write(Path(item["log"]).read_text(encoding="utf-8", errors="replace"))
                combined.write("\n")
                combined.flush()
                write_summary(logs, names, results)
                if item["status"] == "interrupted":
                    return 130
        summary = write_summary(logs, names, results)
        print(json.dumps(summary["counts"], sort_keys=True))
        return 0 if summary["ok"] else 1
    except KeyboardInterrupt:
        print("runner interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print("runner error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
