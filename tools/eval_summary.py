#!/usr/bin/env python3
"""
eval_summary.py -- compare a recent run against prior baseline runs.

Reads the SQLite database populated by tools/eval_runner.py and prints a
Markdown report of per-instance and aggregate differences.

Per instance the report shows:
  - target objective (the run we're inspecting)
  - best objective seen across the baseline window (which run produced it)
  - absolute and percent delta vs that best baseline
  - SA iterations, chosen init heuristic, whether the fallback path triggered

Aggregate footer shows: improvements, regressions, ties, and any feasibility
losses. Objective is a minimization metric -- a negative delta is good.

Usage
-----
    python tools/eval_summary.py
    python tools/eval_summary.py --target-run 5 --baseline-window 3
    python tools/eval_summary.py --instance-pattern "bench_"
    python tools/eval_summary.py --db tools/ogc2026_runs.db
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "tools" / "ogc2026_runs.db"


def fetch_run(conn: sqlite3.Connection, run_id: int):
    cur = conn.cursor()
    cur.execute(
        "SELECT run_id, started_at, git_sha, git_dirty, timelimit, pattern, note "
        "FROM runs WHERE run_id = ?",
        (run_id,),
    )
    return cur.fetchone()


def fetch_results(conn: sqlite3.Connection, run_id: int) -> dict:
    """Return {instance_name: row_tuple} for myalgorithm rows of this run."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT instance, feasible, stage, total_obj, wall_time,
               sa_iterations, sa_improvements, init_heuristic, fallback_triggered
        FROM instance_results
        WHERE run_id = ? AND algo = 'myalgorithm'
        """,
        (run_id,),
    )
    return {r[0]: r for r in cur.fetchall()}


def fetch_recent_run_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    cur = conn.cursor()
    cur.execute("SELECT run_id FROM runs ORDER BY run_id DESC LIMIT ?", (limit,))
    return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(
        description="Summarize an OGC2026 evaluation run vs prior baseline runs."
    )
    ap.add_argument("--db", type=str, default=str(DEFAULT_DB))
    ap.add_argument("--target-run", type=int, default=None,
                    help="run_id to summarize (default: most recent)")
    ap.add_argument("--baseline-window", type=int, default=3,
                    help="How many prior runs to use as the baseline pool (default: 3)")
    ap.add_argument("--instance-pattern", type=str, default=None,
                    help="Substring match against instance name (e.g. 'bench_')")
    args = ap.parse_args()

    db_path = pathlib.Path(args.db)
    if not db_path.exists():
        print(f"DB not found at {db_path}. Run tools/eval_runner.py first.")
        return

    conn = sqlite3.connect(str(db_path))

    recent_ids = fetch_recent_run_ids(conn, limit=args.baseline_window + 5)
    if not recent_ids:
        print("No runs in DB.")
        return

    target_id = args.target_run or recent_ids[0]
    target_row = fetch_run(conn, target_id)
    if not target_row:
        print(f"run_id={target_id} not found.")
        return

    baseline_ids = [r for r in recent_ids if r != target_id][:args.baseline_window]
    target_results = fetch_results(conn, target_id)
    baseline_runs_results: dict[int, dict] = {
        r: fetch_results(conn, r) for r in baseline_ids
    }

    instances = set(target_results.keys())
    for rr in baseline_runs_results.values():
        instances.update(rr.keys())
    if args.instance_pattern:
        needle = args.instance_pattern
        instances = {i for i in instances if needle in i}
    instances = sorted(instances)

    # Header
    print(f"# Run #{target_id} vs baselines {baseline_ids}")
    print()
    sha = target_row[2] or ""
    sha_short = sha[:8] if sha else "n/a"
    print(f"- **Target**: run_id={target_row[0]} started={target_row[1]} "
          f"sha={sha_short}{' (dirty)' if target_row[3] else ''} "
          f"timelimit={target_row[4]}s pattern=`{target_row[5]}`"
          f"{' note=' + repr(target_row[6]) if target_row[6] else ''}")
    if baseline_ids:
        print(f"- **Baseline pool** (most-recent first): {baseline_ids}")
    else:
        print("- **Baseline pool**: _(none — this is the first run)_")
    print()

    headers = ["Instance", "feasible", "obj (target)", "best baseline (run)",
               "Δ vs best", "SA iters", "init", "fb"]
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join(["---"] * len(headers)) + " |")

    n_better = 0
    n_worse = 0
    n_same = 0
    n_new_feasible = 0
    n_lost_feasible = 0

    for inst in instances:
        tr = target_results.get(inst)
        if not tr:
            print(f"| {inst} | _missing in target_ |  |  |  |  |  |  |")
            continue
        (_, t_feasible, t_stage, t_total, _t_wall,
         t_sa, t_sa_imp, t_init, t_fb) = tr

        best_b = None
        best_b_run = None
        for rid, rr in baseline_runs_results.items():
            row = rr.get(inst)
            if not row:
                continue
            b_feasible, b_total = row[1], row[3]
            if b_feasible and b_total is not None:
                if best_b is None or b_total < best_b:
                    best_b = b_total
                    best_b_run = rid

        delta_str = "—"
        if t_feasible and t_total is not None and best_b is not None:
            delta = t_total - best_b
            pct = (delta / best_b * 100.0) if best_b else 0.0
            if delta < -1e-6:
                arrow = "↓ better"
                n_better += 1
            elif delta > 1e-6:
                arrow = "↑ worse"
                n_worse += 1
            else:
                arrow = "·"
                n_same += 1
            delta_str = f"{delta:+.2f} ({pct:+.1f}%) {arrow}"
        elif t_feasible and best_b is None:
            delta_str = "_new feasible_"
            n_new_feasible += 1
        elif (not t_feasible) and best_b is not None:
            delta_str = "_FEASIBILITY LOST_"
            n_lost_feasible += 1

        feas_str = "✓" if t_feasible else (
            f"✗ stage={t_stage}" if t_stage else "✗"
        )
        t_total_str = f"{t_total:.2f}" if t_total is not None else "—"
        best_b_str = f"{best_b:.2f} (run {best_b_run})" if best_b is not None else "—"
        sa_str = (
            f"{t_sa}/{t_sa_imp or 0}" if t_sa is not None else "—"
        )
        init_str = t_init or "—"
        fb_str = "yes" if t_fb else ""

        print(f"| {inst} | {feas_str} | {t_total_str} | {best_b_str} | "
              f"{delta_str} | {sa_str} | {init_str} | {fb_str} |")

    print()
    bits = [
        f"improvements: **{n_better}**",
        f"regressions: **{n_worse}**",
        f"equal: {n_same}",
    ]
    if n_new_feasible:
        bits.append(f"newly feasible: {n_new_feasible}")
    if n_lost_feasible:
        bits.append(f"feasibility LOST: **{n_lost_feasible}**")
    print("**Aggregate** — " + " · ".join(bits))

    conn.close()


if __name__ == "__main__":
    main()
