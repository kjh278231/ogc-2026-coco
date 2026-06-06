"""Isolated place_initial measurement harness (Step 0 verification).

Calls Athena's Phase 1 -> 2 -> 4 (place_initial) directly with deadline=None so
the *initial* solution quality is measured deterministically, with no SA and no
multiprocessing noise. Run on the unedited code to capture a BEFORE snapshot,
then again after editing for the AFTER snapshot, then diff the two JSON files.

Usage (from repo root, PYTHONPATH=.codex_deps):
    py -3.12 .claude/scratch/measure_init.py --out .claude/scratch/init_before.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.getcwd(), "baseline"))

import my_new_algorithm as A  # noqa: E402


def measure(path: str) -> dict:
    prob = json.load(open(path, encoding="utf-8"))
    # Mirror algorithm() setup exactly.
    bays = [A.Bay.from_dict(d, i) for i, d in enumerate(prob["bays"])]
    w1 = float(prob.get("weights", {}).get("w1", 1.0))
    w2 = float(prob.get("weights", {}).get("w2", 1.0))
    w3 = float(prob.get("weights", {}).get("w3", 1.0))
    F = A.precompute_features(prob, bays)
    te, to = A.smooth_time_windows(prob, F)
    t0 = time.time()
    assignments, n_forced = A.place_initial(prob, F, bays, te, to, w1, w2, w3, None)
    dt = time.time() - t0
    res, _sol = A.evaluate_solution(prob, assignments)
    feasible = bool(res["feasible"])
    return {
        "bays": len(bays),
        "blocks": len(prob["blocks"]),
        "feasible": feasible,
        "stage": str(res.get("stage")),
        "objective": float(res["objective"]) if feasible else None,
        "n_forced": int(n_forced),
        "place_s": round(dt, 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    results: dict = {}
    t0 = time.time()
    for f in sorted(glob.glob("alg_tester/example/benchmark/*.json")):
        name = os.path.basename(f)
        try:
            results[name] = measure(f)
        except Exception as e:  # noqa: BLE001
            results[name] = {"error": repr(e)}
    results["_elapsed"] = round(time.time() - t0, 2)

    json.dump(results, open(args.out, "w", encoding="utf-8"), indent=2)

    items = [(k, v) for k, v in results.items() if k != "_elapsed" and isinstance(v, dict)]
    n = len(items)
    feas = sum(1 for _, v in items if v.get("feasible"))
    errs = sum(1 for _, v in items if "error" in v)
    sum_obj = sum(v["objective"] for _, v in items
                  if v.get("feasible") and v.get("objective") is not None)
    tot_forced = sum(v.get("n_forced", 0) for _, v in items if "n_forced" in v)
    print(f"instances={n} feasible={feas} errors={errs} "
          f"sum_obj={sum_obj:.2f} tot_forced={tot_forced} "
          f"wall={results['_elapsed']}s -> {args.out}")


if __name__ == "__main__":
    main()
