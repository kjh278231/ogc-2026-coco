"""Full framework as one callable: assignment search (disjoint-packing eval +
local search) -> assemble -> validate. Batch over all 20 train instances with a
time budget, reporting feasibility / objective / runtime (time-limit compliance).
"""
import sys, os, json, time, glob
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline")); sys.path.insert(0, HERE)
import utils
from prototype import solve_bay, extract_tardiness
from assignment_search import (a_pref, a_balanced_load, a_pref_capped,
                               total_obj, obj23, local_search, improved_search, fits)


def build_solution(prob, assign):
    m = len(prob["bays"]); ops = {}
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        placed = solve_bay(prob, j, ids)
        _, exits = extract_tardiness(prob, j, placed)
        for p in placed:
            en, ex = int(p["entry"]), int(exits[p["id"]])
            ops.setdefault(str(ex), []).append({"type": "EXIT", "block_id": p["id"], "bay_id": j})
            ops.setdefault(str(en), []).append({"type": "ENTRY", "block_id": p["id"], "bay_id": j,
                                                "x": int(p["x"]), "y": int(p["y"]), "orient_idx": p["o"]})
    for k in ops:
        ops[k].sort(key=lambda d: 0 if d["type"] == "EXIT" else 1)
    ops = {k: ops[k] for k in sorted(ops, key=int)}
    return {"operations": ops}


def framework_solve(prob, time_budget):
    t0 = time.time()
    cache = {}
    # two-phase search (big wins on most instances, but hill-climbing can land in a
    # worse local min on a few) ...
    asg_imp, t_imp = improved_search(prob, cache, budget_s=time_budget)
    # ... so also run the proven focused search from the best seed with its OWN full
    # budget (not a split, which would starve both) and keep the better of the two
    # (shared cache). Guarantees we never regress below the validated baseline while
    # keeping the two-phase gains. Total wall ~= 2x budget (fine vs competition limit).
    best_seed, bt = None, float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob); tot, *_ = total_obj(prob, a, cache)
        if tot < bt:
            bt, best_seed = tot, a
    asg_bas, t_bas = local_search(prob, best_seed, cache, budget_s=time_budget)
    best = asg_imp if t_imp <= t_bas else asg_bas
    return build_solution(prob, best)


def run_batch(paths, budget):
    print(f"{'inst':8s} {'n':4s} {'m':2s} {'feas':5s} {'obj':>12s} {'obj1':>6s} "
          f"{'obj2':>7s} {'obj3':>7s} {'sec':>5s}")
    for path in paths:
        prob = json.load(open(path, encoding="utf-8"))
        name = os.path.basename(path).replace(".json", "")
        n = len(prob["blocks"]); m = len(prob["bays"])
        t0 = time.time()
        sol = framework_solve(prob, budget)
        dt = time.time() - t0
        res = utils.check_feasibility(prob, sol)
        if res["feasible"]:
            print(f"{name:8s} {n:<4d} {m:<2d} {'OK':5s} {res['objective']:12.0f} "
                  f"{res['obj1']:6.0f} {res['obj2']:7.0f} {res['obj3']:7.0f} {dt:5.0f}")
        else:
            print(f"{name:8s} {n:<4d} {m:<2d} {'FAIL':5s} stage={res['stage']} "
                  f"{res['violations'][:1]} {dt:5.0f}")


if __name__ == "__main__":
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    pats = sys.argv[2:] or ["train/*.json"]
    paths = []
    for p in pats:
        paths += sorted(glob.glob(p), key=lambda q: int(''.join(c for c in os.path.basename(q) if c.isdigit())))
    run_batch(paths, budget)
