#!/usr/bin/env python3
"""
parallel_eval.py -- run an OGC2026 evaluation across several pinned worker
processes, then merge every result into a SINGLE run_id in the same SQLite DB
that tools/eval_runner.py writes to.

Why this exists
---------------
The Hermes solver is wall-clock-deadline driven and single-threaded: solution
quality == number of SA iterations completed inside `timelimit` seconds. If you
oversubscribe cores, each time-budgeted worker completes FEWER iterations in the
same wall-clock window and reports a worse objective -- and the penalty is
non-uniform across instances, so A/B comparisons silently rot.

This runner avoids that by:
  * process isolation     -- myalgorithm monkey-patches utils.* and mutates
                             os.environ globally; threads would clobber each
                             other, so every worker is a separate process.
  * dedicated core pins   -- each worker gets `--cores-per-worker` (default 4,
                             matching the competition's 4-CPU allotment) cores
                             via CPU affinity, with no overlap by default.
  * headroom              -- default worker count is cpu_count // cores_per_worker
                             (== 6 on a 24-core box), leaving the remaining
                             logical CPUs for the OS so no worker is starved.
  * BLAS thread pinning   -- OMP/MKL/OPENBLAS threads forced to 1 per worker so
                             numpy (under shapely) cannot fan out and contend
                             with neighbours.
  * single-writer merge   -- workers only run the solver and write per-instance
                             JSONL into the shared run_log_dir; the PARENT does
                             every DB INSERT, under one run_id, sequentially.

FAIRNESS RULE (read this): only compare runs produced under the SAME parallel
conditions (same --workers / --cores-per-worker). The contention penalty cancels
in the delta, but a parallel run is NOT comparable to an old serial run.

Usage
-----
    # default: 6 workers x 4 cores on a 24-core machine, merged into the DB
    python tools/parallel_eval.py --timelimit 30 --pattern "prob_*.json" --note "..."

    # override worker count explicitly
    python tools/parallel_eval.py --workers 4 --timelimit 60 --pattern "prob_*.json"
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
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
BENCH_DIR = REPO_ROOT / "training_set"
DEFAULT_DB = TOOLS_DIR / "ogc2026_runs.db"
EVENT_LOG_DIR = TOOLS_DIR / "event_logs"

# Force single-threaded BLAS as early as possible -- must precede any numpy
# import (shapely pulls numpy in). This matters in both parent and worker.
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

sys.path.insert(0, str(TOOLS_DIR))


# --------------------------------------------------------------------------- #
# CPU affinity (best-effort, cross-platform)
# --------------------------------------------------------------------------- #
def pin_to_cores(cores: list[int]) -> str:
    """Pin the current process to `cores`. Returns a human-readable status."""
    if not cores:
        return "no-pin (empty core set)"
    # 1) psutil if present -- cleanest, works on Win + Linux.
    try:
        import psutil  # type: ignore
        psutil.Process().cpu_affinity(cores)
        return f"psutil affinity -> {cores}"
    except Exception:
        pass
    # 2) Windows: SetProcessAffinityMask via ctypes.
    if sys.platform == "win32":
        try:
            import ctypes
            mask = 0
            for c in cores:
                mask |= (1 << c)
            h = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.kernel32.SetProcessAffinityMask(h, ctypes.c_size_t(mask))
            if ok:
                return f"win32 affinity mask 0x{mask:x} -> {cores}"
            return f"win32 SetProcessAffinityMask FAILED for {cores}"
        except Exception as e:
            return f"win32 affinity error: {e!r}"
    # 3) Linux: os.sched_setaffinity.
    try:
        os.sched_setaffinity(0, set(cores))  # type: ignore[attr-defined]
        return f"sched_setaffinity -> {cores}"
    except Exception as e:
        return f"no-pin (affinity unsupported: {e!r})"


def assign_cores(n_workers: int, cores_per_worker: int) -> list[list[int]]:
    """Give each worker a disjoint contiguous block of cores when it fits.

    If workers*cores_per_worker exceeds the logical CPU count we fall back to
    round-robin (cores will overlap -- a warning is printed by the caller).
    """
    total = os.cpu_count() or 1
    if n_workers * cores_per_worker <= total:
        return [list(range(i * cores_per_worker, i * cores_per_worker + cores_per_worker))
                for i in range(n_workers)]
    # Oversubscribed: round-robin assignment so load at least spreads out.
    out: list[list[int]] = []
    for i in range(n_workers):
        out.append([(i * cores_per_worker + k) % total for k in range(cores_per_worker)])
    return out


# --------------------------------------------------------------------------- #
# Worker mode: run a slice of instances, write JSONL into the shared log dir,
# emit a compact per-instance result JSON. NO DB access here.
# --------------------------------------------------------------------------- #
def run_worker(files: list[pathlib.Path], timelimit: float,
               run_log_dir: pathlib.Path, result_file: pathlib.Path,
               cores: list[int], algo: str = "hermes") -> None:
    pin_status = pin_to_cores(cores)
    # Imported here (worker only) so the parent stays light and BLAS env is set.
    import eval_runner  # noqa: E402

    results: dict[str, dict] = {}
    for fp in files:
        name = fp.stem
        log_path = run_log_dir / f"{name}.jsonl"
        t0 = time.time()
        try:
            res = eval_runner.run_instance(fp, timelimit, log_path, algo=algo)
        except Exception as e:  # pragma: no cover -- defensive
            res = {"feasible": None, "stage": None, "obj1": None, "obj2": None,
                   "obj3": None, "total_obj": None, "wall_time": time.time() - t0,
                   "error": f"worker exception: {e!r}"}
        results[name] = res

    result_file.write_text(
        json.dumps({"pin": pin_status, "cores": cores, "results": results}),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Parent mode
# --------------------------------------------------------------------------- #
def get_git_info():
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), text=True).strip()
    except Exception:
        sha = None
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=str(REPO_ROOT), text=True)
        dirty = bool(status.strip())
    except Exception:
        dirty = False
    return sha, dirty


def chunk_round_robin(files: list[pathlib.Path], n: int) -> list[list[pathlib.Path]]:
    """Round-robin split so each worker gets a mix of big/small instances."""
    buckets: list[list[pathlib.Path]] = [[] for _ in range(n)]
    for i, fp in enumerate(files):
        buckets[i % n].append(fp)
    return [b for b in buckets if b]  # drop empties if fewer files than workers


def main() -> None:
    default_workers = max(1, (os.cpu_count() or 4) // 4)
    ap = argparse.ArgumentParser(
        description="Parallel OGC2026 eval, merged into one run_id in SQLite.")
    ap.add_argument("--workers", type=int, default=default_workers,
                    help=f"number of worker processes (default cpu//4 = {default_workers})")
    ap.add_argument("--cores-per-worker", type=int, default=4,
                    help="logical cores pinned per worker (default 4 = competition CPU budget)")
    ap.add_argument("--timelimit", type=float, default=60.0,
                    help="time limit per instance (seconds)")
    ap.add_argument("--pattern", type=str, default="*.json",
                    help="glob pattern (against --bench-dir) selecting instances")
    ap.add_argument("--db", type=str, default=str(DEFAULT_DB), help="SQLite database path")
    ap.add_argument("--note", type=str, default="", help="free-form note attached to this run")
    ap.add_argument("--bench-dir", type=str, default=str(BENCH_DIR),
                    help="directory containing benchmark instance JSON files")
    ap.add_argument("--algo", type=str, default="hermes",
                    help="which solver to run (default hermes); passed through to "
                         "eval_runner.run_instance")
    # Hidden worker-mode flags.
    ap.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--_files", type=str, default="", help=argparse.SUPPRESS)
    ap.add_argument("--_run-log-dir", type=str, default="", help=argparse.SUPPRESS)
    ap.add_argument("--_result-file", type=str, default="", help=argparse.SUPPRESS)
    ap.add_argument("--_cores", type=str, default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._worker:
        files = [pathlib.Path(p) for p in json.loads(args._files)]
        cores = [int(c) for c in args._cores.split(",")] if args._cores else []
        run_worker(files, args.timelimit, pathlib.Path(args._run_log_dir),
                   pathlib.Path(args._result_file), cores, algo=args.algo)
        return

    # ---- parent ----------------------------------------------------------- #
    bench = pathlib.Path(args.bench_dir)
    files = sorted(bench.glob(args.pattern))
    if not files:
        print(f"No instances matched {args.pattern} in {bench}")
        sys.exit(1)

    n_workers = max(1, min(args.workers, len(files)))
    cpw = max(1, args.cores_per_worker)
    total_cores = os.cpu_count() or 1
    if n_workers * cpw > total_cores:
        print(f"[parallel_eval] WARNING: {n_workers} workers x {cpw} cores = "
              f"{n_workers * cpw} > {total_cores} logical cores -- cores will "
              f"OVERLAP (oversubscribed). Objectives may be pessimistic; keep "
              f"the same setting across A/B runs.")

    # Import eval_runner only to reuse its schema/parse helpers in the parent.
    import eval_runner  # noqa: E402

    # Resolve the DB label for the chosen solver (also validates --algo early).
    _, db_label = eval_runner.resolve_algo(args.algo)

    sha, dirty = get_git_info()
    conn = eval_runner.init_db(pathlib.Path(args.db))
    cur = conn.cursor()
    note = args.note or ""
    note = (note + " " if note else "") + f"[parallel workers={n_workers} cpw={cpw} algo={db_label}]"
    cur.execute(
        """INSERT INTO runs (started_at, git_sha, git_dirty, timelimit, pattern,
                             hostname, python_version, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         sha, 1 if dirty else 0, args.timelimit, args.pattern,
         socket.gethostname(), platform.python_version(), note),
    )
    run_id = cur.lastrowid
    conn.commit()

    run_log_dir = EVENT_LOG_DIR / f"run_{run_id}"
    run_log_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = run_log_dir / "_worker_results"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    buckets = chunk_round_robin(files, n_workers)
    core_blocks = assign_cores(len(buckets), cpw)

    print(f"[parallel_eval] run_id={run_id} sha={sha[:8] if sha else 'n/a'}"
          f"{' (dirty)' if dirty else ''} files={len(files)} "
          f"workers={len(buckets)} cores/worker={cpw} timelimit={args.timelimit}s "
          f"db={args.db}")

    # Spawn workers. Each subprocess inherits our env (PYTHONPATH, BLAS=1).
    env = os.environ.copy()
    procs = []
    t0 = time.time()
    for i, bucket in enumerate(buckets):
        cores = core_blocks[i]
        result_file = tmp_dir / f"worker_{i}.json"
        cmd = [
            sys.executable, str(pathlib.Path(__file__).resolve()),
            "--_worker",
            "--timelimit", str(args.timelimit),
            "--_files", json.dumps([str(p) for p in bucket]),
            "--_run-log-dir", str(run_log_dir),
            "--_result-file", str(result_file),
            "--_cores", ",".join(str(c) for c in cores),
            "--algo", args.algo,
        ]
        print(f"[parallel_eval] worker {i}: {len(bucket)} instances -> cores {cores}")
        # Redirect the solver's chatty stdout/stderr to a per-worker file so the
        # parent console stays clean and 6 workers don't interleave garbled text.
        wlog = open(tmp_dir / f"worker_{i}.log", "w", encoding="utf-8")
        procs.append((i, result_file, bucket,
                      subprocess.Popen(cmd, env=env, cwd=str(REPO_ROOT),
                                       stdout=wlog, stderr=subprocess.STDOUT), wlog))

    for i, _rf, _bucket, p, _wlog in procs:
        p.wait()
        _wlog.close()

    wall = time.time() - t0

    # ---- merge: parent is the sole DB writer ------------------------------ #
    inserted = 0
    feasible_ct = 0
    for i, result_file, bucket, p, _wlog in procs:
        if result_file.exists():
            payload = json.loads(result_file.read_text(encoding="utf-8"))
            worker_results = payload.get("results", {})
        else:
            worker_results = {}
        for fp in bucket:
            name = fp.stem
            result = worker_results.get(name)
            if result is None:
                result = {"feasible": None, "stage": "WORKER_LOST",
                          "obj1": None, "obj2": None, "obj3": None,
                          "total_obj": None, "wall_time": None,
                          "error": f"no result from worker {i} (subprocess rc={p.returncode})"}
            log_path = run_log_dir / f"{name}.jsonl"
            events, summary = eval_runner.parse_event_log(log_path)

            cur.execute(
                """INSERT OR REPLACE INTO instance_results
                    (run_id, instance, algo, feasible, stage, obj1, obj2, obj3,
                     total_obj, wall_time, sa_iterations, sa_improvements,
                     init_heuristic, init_objective, fallback_triggered, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    """INSERT INTO events (run_id, instance, algo, t, event, payload)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [(run_id, name, db_label, rec.get("t", 0.0),
                      rec.get("event", ""),
                      json.dumps({k: v for k, v in rec.items() if k not in ("t", "event")}))
                     for rec in events],
                )
            inserted += 1
            if result["feasible"]:
                feasible_ct += 1

            feas = "ok " if result["feasible"] else ("ERR" if result["error"] else "INF")
            obj = f"obj={result['total_obj']:.2f}" if result["total_obj"] is not None else "obj=n/a"
            wt = f"t={result['wall_time']:.2f}s" if result["wall_time"] is not None else "t=n/a"
            print(f"[parallel_eval]   {name}: {feas} {obj} {wt}")
    conn.commit()
    conn.close()

    print(f"[parallel_eval] done. run_id={run_id} instances={inserted} "
          f"feasible={feasible_ct}/{inserted} wall={wall:.1f}s "
          f"(vs ~{len(files) * args.timelimit:.0f}s serial upper bound)")


if __name__ == "__main__":
    main()
