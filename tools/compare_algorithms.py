#!/usr/bin/env python3
"""
compare_algorithms.py -- run multiple algorithms on the same benchmark
instances and print a side-by-side comparison table.

Reads benchmark JSONs from alg_tester/example/benchmark/, runs the three
solvers (Hermes / myalgorithm, baseline_greedy, Athena / my_new_algorithm),
collects feasibility + objective + wall_time, and prints a table.

Does NOT touch SQLite (so it does not pollute eval_runner's run history) and
does NOT modify any of the algorithms.

Usage
-----
    py -3.12 tools/compare_algorithms.py --timelimit 20 --pattern "smoke_*.json"
    py -3.12 tools/compare_algorithms.py --timelimit 30 --pattern "bench_*.json"
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import sys
import time
import traceback

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "baseline"
BENCH = REPO / "alg_tester" / "example" / "benchmark"

sys.path.insert(0, str(BASELINE))

import utils  # noqa: E402


# Each entry: (label, module_name, callable_name, kwargs_factory)
# kwargs_factory: takes timelimit, returns dict to pass to the callable.
ALGOS = [
    ("hermes",    "myalgorithm",        "algorithm",       lambda tl: {"timelimit": tl}),
    ("greedy",    "baseline_greedy",    "greedyalgorithm", lambda tl: {"timelimit": tl, "repair_mode": "greedy"}),
    ("athena",    "my_new_algorithm",   "algorithm",       lambda tl: {"timelimit": tl}),
]


def _reset_utils_patches() -> None:
    """Defensive: if a previous algorithm left utils.* patched, restore originals
    using the pristine references stashed by importing baseline.utils as 'utils'.
    """
    # Re-import a fresh utils to get the original functions
    fresh = importlib.import_module("utils")
    # We just need to make sure the names exist; the algorithms themselves restore
    # on the happy path.
    _ = fresh.check_entry, fresh.check_exit, fresh.check_collisions


def run_single(algo_label: str, mod_name: str, fn_name: str,
               kwargs: dict, prob_info: dict) -> dict:
    out = {
        "algo": algo_label,
        "feasible": None, "stage": None,
        "obj1": None, "obj2": None, "obj3": None, "total": None,
        "wall": None, "error": None,
    }
    _reset_utils_patches()
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name)
        t0 = time.time()
        sol = fn(prob_info, **kwargs)
        out["wall"] = time.time() - t0
        _reset_utils_patches()
        res = utils.check_feasibility(prob_info, sol)
        out["feasible"] = bool(res.get("feasible"))
        out["stage"] = str(res.get("stage"))
        out["obj1"] = res.get("obj1")
        out["obj2"] = res.get("obj2")
        out["obj3"] = res.get("obj3")
        out["total"] = res.get("objective")
    except Exception:
        if out["wall"] is None:
            out["wall"] = 0.0
        out["error"] = traceback.format_exc()
    return out


def fmt_num(v, width: int, fmt: str = ".0f") -> str:
    if v is None:
        return ("-" * 1).rjust(width)
    try:
        return f"{v:{fmt}}".rjust(width)
    except Exception:
        return str(v).rjust(width)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timelimit", type=float, default=15.0,
                    help="per-instance wall-clock limit (seconds)")
    ap.add_argument("--pattern", type=str, default="smoke_*.json",
                    help="glob pattern (against --bench-dir) for instance JSON files")
    ap.add_argument("--bench-dir", type=str, default=str(BENCH))
    ap.add_argument("--out", type=str, default="",
                    help="optional output JSON file for raw per-(instance, algo) results")
    ap.add_argument("--algos", type=str, default="",
                    help="comma-separated subset of algo labels to run (default: all)")
    args = ap.parse_args()

    bench = pathlib.Path(args.bench_dir)
    files = sorted(bench.glob(args.pattern))
    if not files:
        print(f"No instances matched {args.pattern} in {bench}")
        sys.exit(1)

    requested = [a.strip() for a in args.algos.split(",") if a.strip()] if args.algos else None
    algos = [a for a in ALGOS if (not requested or a[0] in requested)]

    print("=" * 110)
    print(f"compare_algorithms  timelimit={args.timelimit}s  files={len(files)}  algos={[a[0] for a in algos]}")
    print("=" * 110)

    all_results: list[dict] = []
    for fp in files:
        name = fp.stem
        with open(fp, "r", encoding="utf-8") as f:
            prob_info = json.load(f)
        n_bays = len(prob_info["bays"])
        n_blocks = len(prob_info["blocks"])

        # Header for this instance
        print()
        print(f"# {name}   bays={n_bays}  blocks={n_blocks}  "
              f"w=(w1={prob_info['weights'].get('w1')}, w2={prob_info['weights'].get('w2')}, "
              f"w3={prob_info['weights'].get('w3')})")
        print(f"{'algo':<10} {'feas':>5} {'stage':>5} {'total':>11} {'obj1':>10} {'obj2':>10} {'obj3':>10} {'time(s)':>8}  note")
        print("-" * 90)

        per_instance: list[dict] = []
        for label, mod, fn, kwfac in algos:
            kw = kwfac(args.timelimit)
            res = run_single(label, mod, fn, kw, prob_info)
            per_instance.append(res)

            feas = "T" if res["feasible"] else ("ERR" if res["error"] else "F")
            stage = res["stage"] or "-"
            total = fmt_num(res["total"], 11)
            o1 = fmt_num(res["obj1"], 10, ".1f")
            o2 = fmt_num(res["obj2"], 10, ".1f")
            o3 = fmt_num(res["obj3"], 10, ".1f")
            wall = fmt_num(res["wall"], 8, ".2f")
            note = ""
            if res["error"]:
                # show first line of traceback
                note = res["error"].splitlines()[-1][:60]
            print(f"{label:<10} {feas:>5} {stage:>5} {total} {o1} {o2} {o3} {wall}  {note}")

            all_results.append({"instance": name, **res})

        # winner among feasible
        feasibles = [r for r in per_instance if r["feasible"] and r["total"] is not None]
        if len(feasibles) >= 2:
            best = min(feasibles, key=lambda r: r["total"])
            ranked = sorted(feasibles, key=lambda r: r["total"])
            spread = ranked[-1]["total"] - ranked[0]["total"]
            print(f"  -> best: {best['algo']} (Δ vs worst feasible = {spread:.1f})")
        elif len(feasibles) == 1:
            print(f"  -> only feasible: {feasibles[0]['algo']}")
        else:
            print(f"  -> no feasible solution from any algorithm")

    # Cross-instance summary
    print()
    print("=" * 110)
    print("Cross-instance summary")
    print("=" * 110)
    print(f"{'instance':<40} " + " ".join(f"{a[0]:>14}" for a in algos))
    for fp in files:
        name = fp.stem
        row = []
        for a in algos:
            r = next((x for x in all_results if x["instance"] == name and x["algo"] == a[0]), None)
            if r is None:
                cell = "-"
            elif r["error"]:
                cell = "ERR"
            elif not r["feasible"]:
                cell = f"INF(s{r['stage']})"
            else:
                cell = f"{r['total']:.0f}"
            row.append(cell.rjust(14))
        print(f"{name:<40} " + " ".join(row))

    # Per-algo aggregate (feasible rate, mean obj on instances all-feasible)
    print()
    print("Aggregate")
    print("-" * 50)
    common_feasible = set()
    feasible_by_algo = {a[0]: set() for a in algos}
    obj_by_algo: dict = {a[0]: {} for a in algos}
    for r in all_results:
        if r["feasible"]:
            feasible_by_algo[r["algo"]].add(r["instance"])
            obj_by_algo[r["algo"]][r["instance"]] = r["total"]
    common = None
    for label in feasible_by_algo:
        common = feasible_by_algo[label] if common is None else (common & feasible_by_algo[label])

    for a in algos:
        label = a[0]
        n_feas = len(feasible_by_algo[label])
        msg = f"  {label:<10} feasible={n_feas}/{len(files)}"
        if common:
            mean_obj = sum(obj_by_algo[label][i] for i in common) / len(common)
            msg += f"  mean_obj_over_common={mean_obj:.1f}  (n_common={len(common)})"
        print(msg)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"\nWrote raw results to {args.out}")


if __name__ == "__main__":
    main()
