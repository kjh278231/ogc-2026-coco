#!/usr/bin/env python3
"""
check_env.py -- One-shot verification that the local Python environment can
run the OGC2026 codebase.

Run with:
    py -3.12 tools/check_env.py

Prints a checklist of:
  - Python interpreter version (must be 3.12.x)
  - PYTHONPATH / PYTHONIOENCODING env vars
  - Importability of the core deps (shapely, numpy)
  - Importability of the project modules (utils, baseline_greedy, myalgorithm)
  - Presence of mcp-server-sqlite (optional, for the MCP path)
  - Presence of the SQLite DB and event-log directory

Each line ends with one of:
    OK         everything fine
    WARN       non-fatal, document why
    FAIL       something must be fixed before tools/eval_runner.py will work

Exit code: 0 if no FAILs, 1 if any FAIL.
"""
from __future__ import annotations

import io
import os
import pathlib
import sys

# Force UTF-8 stdout so the report's em-dashes / Korean don't crash on cp949.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

REPO = pathlib.Path(__file__).resolve().parent.parent
fail_count = 0
warn_count = 0


def line(label: str, status: str, detail: str = "") -> None:
    global fail_count, warn_count
    status = status.strip().upper()
    pad = max(1, 38 - len(label))
    msg = f"  {label}{' ' * pad}[{status}]"
    if detail:
        msg += f"  {detail}"
    print(msg)
    if status == "FAIL":
        fail_count += 1
    elif status == "WARN":
        warn_count += 1


def section(title: str) -> None:
    print()
    print(f"== {title} ==")


def main() -> int:
    print("OGC2026 Environment Check")
    print(f"Repo root: {REPO}")
    print(f"Working dir: {pathlib.Path.cwd()}")

    # ---- Python interpreter ----
    section("Python interpreter")
    v = sys.version_info
    py_version = f"{v.major}.{v.minor}.{v.micro}"
    if v.major == 3 and v.minor == 12:
        line(f"Python version = {py_version}", "OK")
    elif v.major == 3 and v.minor in (10, 11, 13):
        line(f"Python version = {py_version}", "FAIL",
             "need 3.12 (codex_deps shapely is cp312); call `py -3.12 ...` instead")
    else:
        line(f"Python version = {py_version}", "FAIL", "need 3.12")
    line(f"Executable: {sys.executable}", "OK")

    # ---- Environment variables ----
    section("Environment variables")
    pp = os.environ.get("PYTHONPATH", "")
    ioenc = os.environ.get("PYTHONIOENCODING", "")
    codex = REPO / ".codex_deps"

    if codex.exists():
        # codex_deps exists -> path B is in use, expect PYTHONPATH set
        if str(codex) in pp or ".codex_deps" in pp:
            line("PYTHONPATH includes .codex_deps", "OK", pp)
        else:
            line("PYTHONPATH includes .codex_deps", "WARN",
                 "not set; set $env:PYTHONPATH=\"<repo>\\.codex_deps\" (path B) "
                 "OR ignore if you're in a conda env (path A)")
    else:
        line("PYTHONPATH includes .codex_deps", "WARN",
             ".codex_deps/ does not exist; only path A (conda) is available")

    if ioenc.lower() == "utf-8":
        line("PYTHONIOENCODING = utf-8", "OK")
    else:
        line("PYTHONIOENCODING = utf-8", "WARN",
             "not set; em-dash / Korean output may crash on Windows cp949 consoles")

    # ---- Core deps importability ----
    section("Core deps")
    # If codex_deps exists and isn't on sys.path, splice it in for the test.
    if codex.exists() and str(codex) not in sys.path:
        sys.path.insert(0, str(codex))

    for mod_name in ("shapely", "numpy"):
        try:
            m = __import__(mod_name)
            ver = getattr(m, "__version__", "?")
            origin = getattr(m, "__file__", "?")
            origin_short = origin.replace(str(REPO), "<repo>") if origin else "?"
            line(f"import {mod_name}  (v{ver})", "OK", origin_short)
        except ImportError as e:
            line(f"import {mod_name}", "FAIL", str(e))
        except Exception as e:
            line(f"import {mod_name}", "FAIL", f"{type(e).__name__}: {e}")

    # ---- Project modules ----
    section("Project modules")
    baseline_dir = REPO / "baseline"
    if str(baseline_dir) not in sys.path:
        sys.path.insert(0, str(baseline_dir))
    for mod_name in ("utils", "baseline_greedy", "myalgorithm"):
        try:
            m = __import__(mod_name)
            origin = getattr(m, "__file__", "?")
            origin_short = origin.replace(str(REPO), "<repo>") if origin else "?"
            line(f"import {mod_name}", "OK", origin_short)
        except ImportError as e:
            line(f"import {mod_name}", "FAIL", str(e))
        except Exception as e:
            line(f"import {mod_name}", "FAIL", f"{type(e).__name__}: {e}")

    # ---- MCP sqlite server (optional) ----
    section("MCP server (optional)")
    try:
        __import__("mcp_server_sqlite")
        line("import mcp_server_sqlite", "OK", "ogc2026-db MCP available")
    except ImportError:
        line("import mcp_server_sqlite", "WARN",
             "not installed; `py -3.12 -m pip install --user mcp-server-sqlite`")

    # ---- DB and event logs ----
    section("Eval data")
    db_path = REPO / "tools" / "ogc2026_runs.db"
    if db_path.exists():
        size_kb = db_path.stat().st_size / 1024
        line(f"tools/ogc2026_runs.db ({size_kb:.1f} KB)", "OK")
    else:
        line("tools/ogc2026_runs.db", "WARN",
             "no eval data yet; will be created on first eval_runner.py run")

    elog_dir = REPO / "tools" / "event_logs"
    if elog_dir.exists():
        n_runs = sum(1 for p in elog_dir.iterdir() if p.is_dir())
        line(f"tools/event_logs/  ({n_runs} run dirs)", "OK")
    else:
        line("tools/event_logs/", "WARN", "will be created on first eval_runner.py run")

    # ---- Benchmark instances ----
    section("Benchmark instances")
    bench_dir = REPO / "training_set"
    if bench_dir.exists():
        n_files = sum(1 for p in bench_dir.glob("prob_*.json"))
        if n_files > 0:
            line(f"training_set/  ({n_files} prob_*.json)", "OK")
        else:
            line("training_set/", "WARN",
                 "no benchmark instances found; expected training_set/prob_*.json")
    else:
        line("training_set/", "FAIL",
             "directory missing; benchmark instances should live here")

    # ---- Summary ----
    print()
    print("=" * 60)
    if fail_count == 0 and warn_count == 0:
        print(f"All checks passed. Ready to run.")
        return 0
    elif fail_count == 0:
        print(f"OK with {warn_count} warning(s). Most commands will work; see notes above.")
        return 0
    else:
        print(f"FAILED: {fail_count} blocker(s), {warn_count} warning(s).")
        print(f"See ENVIRONMENT.md §4 Troubleshooting for fixes.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
