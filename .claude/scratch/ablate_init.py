"""Ablation of Step 0 (1-A z2-weighting, 1-C distinct-bay cap) on place_initial.

Measures pure initial-solution quality (deadline=None, no SA, no multiprocessing)
under four toggle combinations, via monkeypatch so production code is untouched:

  baseline    : 1-A off, 1-C off  -> must reproduce the pre-edit numbers
  cap_only    : 1-A off, 1-C on
  weight_only : 1-A on,  1-C off
  both        : 1-A on,  1-C on   (== current committed behaviour)

1-A off  := A._bay_weights returns None  (rank/_placement_score take legacy path)
1-C off  := A._top_bays returns ranked[:4] (original orient-mixed slice, cap=4)

Usage (repo root, PYTHONPATH=.codex_deps):
    py -3.12 .claude/scratch/ablate_init.py --out .claude/scratch/ablation.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.getcwd(), "baseline"))
import my_new_algorithm as A  # noqa: E402

MODES = ["baseline", "cap_only", "weight_only", "both"]
ORIG_CAP = 4  # original bay_cands_cap default


def measure(path: str, mode: str) -> dict:
    prob = json.load(open(path, encoding="utf-8"))
    bays = [A.Bay.from_dict(d, i) for i, d in enumerate(prob["bays"])]
    w1 = float(prob.get("weights", {}).get("w1", 1.0))
    w2 = float(prob.get("weights", {}).get("w2", 1.0))
    w3 = float(prob.get("weights", {}).get("w3", 1.0))
    F = A.precompute_features(prob, bays)
    te, to = A.smooth_time_windows(prob, F)

    orig_bw = A._bay_weights
    orig_tb = A._top_bays
    try:
        if mode in ("baseline", "cap_only"):
            A._bay_weights = lambda bays: None            # 1-A off
        if mode in ("baseline", "weight_only"):
            A._top_bays = lambda ranked, cap: ranked[:ORIG_CAP]  # 1-C off
        t0 = time.time()
        assignments, n_forced = A.place_initial(prob, F, bays, te, to, w1, w2, w3, None)
        dt = time.time() - t0
    finally:
        A._bay_weights = orig_bw
        A._top_bays = orig_tb

    res, _ = A.evaluate_solution(prob, assignments)
    feasible = bool(res["feasible"])
    return {
        "bays": len(bays),
        "blocks": len(prob["blocks"]),
        "feasible": feasible,
        "objective": float(res["objective"]) if feasible else None,
        "n_forced": int(n_forced),
        "place_s": round(dt, 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    files = sorted(glob.glob("alg_tester/example/benchmark/*.json"))
    out: dict = {m: {} for m in MODES}
    for m in MODES:
        for f in files:
            name = os.path.basename(f)
            try:
                out[m][name] = measure(f, m)
            except Exception as e:  # noqa: BLE001
                out[m][name] = {"error": repr(e)}
    json.dump(out, open(args.out, "w", encoding="utf-8"), indent=2)

    base = out["baseline"]

    def sum_obj(d):
        return sum(v["objective"] for v in d.values()
                   if v.get("feasible") and v.get("objective") is not None)

    base_sum = sum_obj(base)
    print(f"{'mode':12s} {'feas':>4s} {'sum_obj':>16s} {'vs_baseline':>12s}")
    for m in MODES:
        d = out[m]
        feas = sum(1 for v in d.values() if v.get("feasible"))
        s = sum_obj(d)
        pct = 100 * (s - base_sum) / base_sum if base_sum else 0.0
        print(f"{m:12s} {feas:>4d} {s:>16.2f} {pct:>+11.3f}%")

    # per-instance, all modes, to localise the crane_trap regression
    print("\nper-instance objective (baseline -> cap_only / weight_only / both):")
    for name in sorted(base):
        b = base[name].get("objective")
        if b is None:
            print(f"  {name}: baseline infeasible")
            continue

        def cell(m):
            o = out[m][name].get("objective")
            if o is None:
                return "INFEAS"
            return f"{100*(o-b)/b:+.1f}%"
        print(f"  {name:38s} base={b:14.2f}  "
              f"cap={cell('cap_only'):>8s}  "
              f"wgt={cell('weight_only'):>8s}  "
              f"both={cell('both'):>8s}")


if __name__ == "__main__":
    main()
