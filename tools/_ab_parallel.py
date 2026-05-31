"""A/B: single-start SA (workers=1) vs parallel multi-start (workers=4).

Fair comparison: identical instance + identical timelimit for both configs.
Several repeats per (instance, config) with independent base seeds, since SA
is stochastic. Reports per-config objective distribution and the win/loss.

    py -3.12 tools/_ab_parallel.py [timelimit] [repeats]
"""
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "baseline"))

INSTANCES = [
    "bench_B3_b30_balanced.json",
    "bench_B3_b50_tight_due.json",
    "bench_B4_b70_dense_geometry.json",
    "bench_B4_b90_crane_trap.json",
    "bench_B5_b120_preference_skew.json",
    "bench_B5_b150_mixed_hard.json",
    "my_B5_b200_hard.json",
]


def run_once(alg, check_feasibility, prob_info, timelimit, workers, seed,
             mode="diverse"):
    os.environ["OGC2026_SA_WORKERS"] = str(workers)
    os.environ["OGC2026_SA_BASE_SEED"] = str(seed)
    os.environ["OGC2026_SA_PROFILE_MODE"] = mode
    os.environ.pop("OGC2026_EVENT_LOG", None)
    t0 = time.time()
    sol = alg.algorithm(prob_info, timelimit)
    dt = time.time() - t0
    res = check_feasibility(prob_info, sol)
    obj = float(res["objective"]) if res["feasible"] else float("inf")
    return obj, bool(res["feasible"]), dt


def main():
    import my_new_algorithm as alg
    from utils import check_feasibility

    timelimit = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    base = os.path.join(os.path.dirname(__file__), "..", "alg_tester",
                        "example", "benchmark")
    print(f"cpu_count={os.cpu_count()} timelimit={timelimit} repeats={repeats}\n")

    # configs: ("label", workers, mode). Only single vs diverse-4 now.
    configs = [
        ("single", 1, "diverse"),
        ("div4", 4, "diverse"),
    ]

    all_pairs = []          # per (instance, seed): div4 % improvement vs single
    per_inst_means = []     # (inst, single_mean, div4_mean, delta%)

    for inst in INSTANCES:
        path = os.path.join(base, inst)
        if not os.path.exists(path):
            print(f"  !! missing {inst}, skipping")
            continue
        with open(path, encoding="utf-8") as fh:
            prob_info = json.load(fh)

        rows = {c[0]: [] for c in configs}
        walls = {c[0]: [] for c in configs}
        for rep in range(repeats):
            seed = 1000 + rep * 101
            by_seed = {}
            for label, workers, mode in configs:
                obj, feas, dt = run_once(alg, check_feasibility, prob_info,
                                         timelimit, workers, seed, mode)
                rows[label].append(obj)
                walls[label].append(dt)
                by_seed[label] = obj
                tag = "OK " if feas else "INF"
                print(f"  {inst:38s} {label:7s} seed={seed} "
                      f"{tag} obj={obj:12.2f} wall={dt:5.2f}", flush=True)
            s, d = by_seed.get("single"), by_seed.get("div4")
            if s not in (None, float("inf")) and d not in (None, float("inf")):
                all_pairs.append((s - d) / s * 100.0)
        print(flush=True)

        def fin(vals):
            return [v for v in vals if v != float("inf")]

        def summ(vals):
            f = fin(vals)
            if not f:
                return "all-infeasible"
            return (f"best={min(f):12.2f} mean={statistics.mean(f):12.2f} "
                    f"worst={max(f):12.2f} (n={len(f)}/{len(vals)})")

        print(f"  [{inst}]")
        sm = statistics.mean(fin(rows["single"]) or [float("inf")])
        dm = statistics.mean(fin(rows["div4"]) or [float("inf")])
        for label, _, _ in configs:
            print(f"    {label:7s}: {summ(rows[label])}")
        delta = None
        if sm != float("inf") and dm != float("inf"):
            delta = (sm - dm) / sm * 100.0
            print(f"    div4 vs single (mean): {delta:+.2f}%")
        per_inst_means.append((inst, sm, dm, delta))
        print(f"    avg wall: " +
              " ".join(f"{c[0]}={statistics.mean(walls[c[0]]):.2f}s" for c in configs))
        print("=" * 70, flush=True)

    # ---- global aggregate ------------------------------------------------
    print("\n" + "#" * 70)
    print("AGGREGATE  (div4 vs single, paired by instance+seed)")
    print("#" * 70)
    if all_pairs:
        wins = sum(1 for d in all_pairs if d > 0.05)
        losses = sum(1 for d in all_pairs if d < -0.05)
        ties = len(all_pairs) - wins - losses
        print(f"  paired runs : {len(all_pairs)}")
        print(f"  div4 wins   : {wins}  losses: {losses}  ~ties: {ties}")
        print(f"  mean   Δ%   : {statistics.mean(all_pairs):+.2f}%")
        print(f"  median Δ%   : {statistics.median(all_pairs):+.2f}%")
        if len(all_pairs) > 1:
            print(f"  stdev  Δ%   : {statistics.pstdev(all_pairs):.2f}")
        print(f"  range  Δ%   : [{min(all_pairs):+.2f}, {max(all_pairs):+.2f}]")
    print("\n  per-instance mean improvement (div4 vs single):")
    for inst, sm, dm, delta in per_inst_means:
        ds = f"{delta:+.2f}%" if delta is not None else "n/a"
        print(f"    {inst:38s} {ds}")


if __name__ == "__main__":
    main()
