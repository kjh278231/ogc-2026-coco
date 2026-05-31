#!/usr/bin/env python3
"""
eval_runner.py -- orchestrate an OGC2026 evaluation run and persist results.

Stores each run's metadata, per-instance results, and structured JSONL events
in SQLite (default: tools/ogc2026_runs.db). Pair with tools/eval_summary.py
to compare consecutive runs and detect regressions.

Usage
-----
    python tools/eval_runner.py --timelimit 60 --pattern "bench_*.json"
    python tools/eval_runner.py --timelimit 30 --pattern "smoke_*.json" --note "post track A"
    python tools/eval_runner.py --timelimit 60 --pattern "my_B5_b200_hard.json" --note "track A regress check"

The runner sets the OGC2026_EVENT_LOG environment variable to a per-instance
JSONL path before calling myalgorithm.algorithm(), then parses the resulting
log to extract init_heuristic / sa_iterations / fallback_triggered / etc.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE_DIR = REPO_ROOT / "baseline"
BENCH_DIR = REPO_ROOT / "alg_tester" / "example" / "benchmark"
DEFAULT_DB = REPO_ROOT / "tools" / "ogc2026_runs.db"
EVENT_LOG_DIR = REPO_ROOT / "tools" / "event_logs"

# Import contestant code from baseline/.
sys.path.insert(0, str(BASELINE_DIR))

import myalgorithm  # noqa: E402
import utils  # noqa: E402


# --------------------------------------------------------------------------- #
# Algorithm registry. Each entry maps a CLI label to the module that exports
# `algorithm(prob_info, timelimit=...)` and the `algo` string recorded in the DB.
# `hermes`/`myalgorithm` share the historical DB label "myalgorithm" so existing
# baseline history and the approval gate keep matching. Athena gets its own label
# so its results never pollute the Hermes pool. Modules are imported lazily so a
# Hermes-only run never touches my_new_algorithm (and vice versa).
# --------------------------------------------------------------------------- #
ALGOS = {
    "hermes": {"module": "myalgorithm", "db_label": "myalgorithm"},
    "myalgorithm": {"module": "myalgorithm", "db_label": "myalgorithm"},
    "athena": {"module": "my_new_algorithm", "db_label": "athena"},
}


def resolve_algo(algo: str):
    """Return (module, db_label) for a CLI algo label. Imports the module lazily."""
    import importlib
    try:
        spec = ALGOS[algo]
    except KeyError:
        raise SystemExit(
            f"unknown --algo {algo!r}; choose one of {sorted(ALGOS)}")
    return importlib.import_module(spec["module"]), spec["db_label"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    git_sha TEXT,
    git_dirty INTEGER,
    timelimit REAL NOT NULL,
    pattern TEXT,
    hostname TEXT,
    python_version TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS instance_results (
    run_id INTEGER NOT NULL,
    instance TEXT NOT NULL,
    algo TEXT NOT NULL,
    feasible INTEGER,
    stage TEXT,
    obj1 REAL,
    obj2 REAL,
    obj3 REAL,
    total_obj REAL,
    wall_time REAL,
    sa_iterations INTEGER,
    sa_improvements INTEGER,
    init_heuristic TEXT,
    init_objective REAL,
    fallback_triggered INTEGER,
    error TEXT,
    PRIMARY KEY (run_id, instance, algo),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS events (
    run_id INTEGER NOT NULL,
    instance TEXT NOT NULL,
    algo TEXT NOT NULL,
    t REAL NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_results_instance ON instance_results(instance, algo);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""


def get_git_info() -> tuple[str | None, bool]:
    """Return (HEAD sha, dirty?). Falls back to (None, False) if git isn't usable."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), text=True
        ).strip()
    except Exception:
        sha = None
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=str(REPO_ROOT), text=True
        )
        dirty = bool(status.strip())
    except Exception:
        dirty = False
    return sha, dirty


def init_db(db_path: pathlib.Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def parse_event_log(path: pathlib.Path) -> tuple[list[dict], dict]:
    """Read JSONL events and extract summary fields for instance_results.

    Returns (events, summary) where summary has the keys expected by
    instance_results: init_heuristic, init_objective, sa_iterations,
    sa_improvements, fallback_triggered.
    """
    events: list[dict] = []
    summary: dict = {
        "init_heuristic": None,
        "init_objective": None,
        "sa_iterations": None,
        "sa_improvements": None,
        "fallback_triggered": 0,
    }
    if not path.exists():
        return events, summary
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            events.append(rec)
            ev = rec.get("event", "")
            if ev == "init.chosen":
                summary["init_heuristic"] = rec.get("name")
                summary["init_objective"] = rec.get("objective")
            elif ev == "init.fallback":
                # Flag the worse path even if the EDD retry happens to succeed,
                # because triggering it means we burned the whole init budget.
                summary["fallback_triggered"] = 1
                if summary["init_heuristic"] is None:
                    summary["init_heuristic"] = f"FALLBACK:{rec.get('path', '?')}"
            elif ev == "init.fallback.outcome":
                summary["init_objective"] = rec.get("objective")
            elif ev == "sa.complete":
                summary["sa_iterations"] = rec.get("iterations")
                summary["sa_improvements"] = rec.get("improvements")
    return events, summary


def run_instance(instance_path: pathlib.Path, timelimit: float,
                 event_log_path: pathlib.Path, algo: str = "hermes") -> dict:
    """Run the selected solver on one instance and return a result dict ready
    for DB insertion. `algo` defaults to "hermes" so existing callers are
    unchanged."""
    algo_mod, _ = resolve_algo(algo)
    with open(instance_path, "r", encoding="utf-8") as f:
        prob_info = json.load(f)

    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    # Start fresh: emit helper opens in append mode, so we clear any stale data.
    event_log_path.write_text("", encoding="utf-8")

    prev_env = os.environ.get("OGC2026_EVENT_LOG")
    os.environ["OGC2026_EVENT_LOG"] = str(event_log_path)

    result = {
        "feasible": None, "stage": None,
        "obj1": None, "obj2": None, "obj3": None, "total_obj": None,
        "wall_time": None, "error": None,
    }
    t0 = time.time()
    try:
        sol = algo_mod.algorithm(prob_info, timelimit=timelimit)
        result["wall_time"] = time.time() - t0

        # Hermes monkey-patches utils.* and restores on the happy path; defensively
        # reset here in case an exception left them dangling. Athena does not patch
        # utils globally (it imports the checkers by name), so only solvers that
        # captured originals get reset.
        if hasattr(algo_mod, "original_check_entry"):
            utils.check_entry = algo_mod.original_check_entry
            utils.check_exit = algo_mod.original_check_exit
            utils.check_collisions = algo_mod.original_check_collisions

        chk = utils.check_feasibility(prob_info, sol)
        result["feasible"] = bool(chk.get("feasible"))
        result["stage"] = str(chk.get("stage"))
        result["obj1"] = chk.get("obj1")
        result["obj2"] = chk.get("obj2")
        result["obj3"] = chk.get("obj3")
        result["total_obj"] = chk.get("objective")
    except Exception:
        result["wall_time"] = time.time() - t0
        result["error"] = traceback.format_exc()
    finally:
        if prev_env is None:
            os.environ.pop("OGC2026_EVENT_LOG", None)
        else:
            os.environ["OGC2026_EVENT_LOG"] = prev_env

    return result


def main():
    ap = argparse.ArgumentParser(description="Run OGC2026 evaluation and persist to SQLite.")
    ap.add_argument("--timelimit", type=float, default=60.0,
                    help="time limit per instance (seconds)")
    ap.add_argument("--pattern", type=str, default="*.json",
                    help="glob pattern (against --bench-dir) selecting instances")
    ap.add_argument("--db", type=str, default=str(DEFAULT_DB),
                    help="SQLite database path")
    ap.add_argument("--note", type=str, default="",
                    help="free-form note attached to this run (shown in summaries)")
    ap.add_argument("--bench-dir", type=str, default=str(BENCH_DIR),
                    help="directory containing benchmark instance JSON files")
    ap.add_argument("--algo", type=str, default="hermes",
                    choices=sorted(ALGOS),
                    help="which solver to run (default hermes)")
    args = ap.parse_args()

    _, db_label = resolve_algo(args.algo)

    bench = pathlib.Path(args.bench_dir)
    files = sorted(bench.glob(args.pattern))
    if not files:
        print(f"No instances matched {args.pattern} in {bench}")
        sys.exit(1)

    sha, dirty = get_git_info()
    conn = init_db(pathlib.Path(args.db))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO runs
            (started_at, git_sha, git_dirty, timelimit, pattern,
             hostname, python_version, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         sha, 1 if dirty else 0, args.timelimit, args.pattern,
         socket.gethostname(), platform.python_version(), args.note),
    )
    run_id = cur.lastrowid
    conn.commit()
    print(f"[eval_runner] run_id={run_id} sha={sha[:8] if sha else 'n/a'}"
          f"{' (dirty)' if dirty else ''} files={len(files)} algo={db_label} db={args.db}")

    run_log_dir = EVENT_LOG_DIR / f"run_{run_id}"
    run_log_dir.mkdir(parents=True, exist_ok=True)

    for fp in files:
        name = fp.stem
        log_path = run_log_dir / f"{name}.jsonl"
        print(f"[eval_runner] {name} ...", end=" ", flush=True)
        result = run_instance(fp, args.timelimit, log_path, algo=args.algo)
        events, summary = parse_event_log(log_path)

        cur.execute(
            """
            INSERT OR REPLACE INTO instance_results
                (run_id, instance, algo, feasible, stage, obj1, obj2, obj3,
                 total_obj, wall_time, sa_iterations, sa_improvements,
                 init_heuristic, init_objective, fallback_triggered, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, name, db_label,
             None if result["feasible"] is None else int(result["feasible"]),
             result["stage"], result["obj1"], result["obj2"], result["obj3"],
             result["total_obj"], result["wall_time"],
             summary["sa_iterations"], summary["sa_improvements"],
             summary["init_heuristic"], summary["init_objective"],
             summary["fallback_triggered"], result["error"]),
        )
        if events:
            cur.executemany(
                """
                INSERT INTO events
                    (run_id, instance, algo, t, event, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(run_id, name, db_label,
                  rec.get("t", 0.0), rec.get("event", ""),
                  json.dumps({k: v for k, v in rec.items() if k not in ("t", "event")}))
                 for rec in events],
            )
        conn.commit()

        feas_marker = "ok " if result["feasible"] else ("ERR" if result["error"] else "INF")
        obj_str = f"obj={result['total_obj']:.2f}" if result["total_obj"] is not None else "obj=n/a"
        bits = [feas_marker, obj_str, f"t={result['wall_time']:.2f}s"]
        if summary["sa_iterations"] is not None:
            bits.append(f"sa={summary['sa_iterations']}/{summary['sa_improvements'] or 0}")
        if summary["init_heuristic"]:
            bits.append(f"init={summary['init_heuristic']}")
        if summary["fallback_triggered"]:
            bits.append("FALLBACK")
        print(" ".join(bits))

    conn.close()
    print(f"[eval_runner] done. run_id={run_id}")


if __name__ == "__main__":
    main()
