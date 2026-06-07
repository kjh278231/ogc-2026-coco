"""Measurement instrument: run organizer baseline greedy on train instances,
report feasibility, objective split, and PER-BAY tardiness breakdown.

This is NOT framework code -- it uses the neutral baseline solely to measure
how large tardiness actually is and how it distributes across bays.
"""
import sys, os, json, time, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline"))

import baseline_greedy  # noqa
import utils  # noqa


def per_bay_tardiness(prob, solution):
    blocks = prob["blocks"]
    entry = {}; exit_ = {}; bay = {}
    for t_str, ops in solution["operations"].items():
        t = int(t_str)
        for op in ops:
            bid = op["block_id"]
            if op["type"] == "ENTRY":
                entry[bid] = t; bay[bid] = op["bay_id"]
            else:
                exit_[bid] = t
    m = len(prob["bays"])
    tard = [0.0] * m
    cnt = [0] * m
    for bid in range(len(blocks)):
        if bid in exit_ and bid in bay:
            tj = max(0.0, exit_[bid] - blocks[bid]["due_date"])
            tard[bay[bid]] += tj
            cnt[bay[bid]] += 1
            if tj > 0:
                pass
    return tard, cnt


def run(path, timelimit):
    prob = json.load(open(path, encoding="utf-8"))
    t0 = time.time()
    sol = baseline_greedy.greedyalgorithm(prob, timelimit)
    elapsed = time.time() - t0
    res = utils.check_feasibility(prob, sol)
    name = os.path.basename(path)
    if not res["feasible"]:
        print(f"{name:10s} INFEASIBLE stage={res['stage']} ({elapsed:.1f}s) "
              f"{res['violations'][:1]}")
        return
    tard, cnt = per_bay_tardiness(prob, sol)
    n_late = sum(1 for bid in range(len(prob['blocks']))
                 if True)  # placeholder
    bay_str = "  ".join(f"b{j}:{tard[j]:.0f}/{cnt[j]}" for j in range(len(tard)))
    print(f"{name:10s} feas obj1(T)={res['obj1']:.0f} obj2={res['obj2']:.0f} "
          f"obj3={res['obj3']:.0f} | per-bay T/cnt: {bay_str} | {elapsed:.1f}s")


if __name__ == "__main__":
    tl = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    pats = sys.argv[2:] if len(sys.argv) > 2 else ["train/prob_1.json"]
    paths = []
    for p in pats:
        paths += sorted(glob.glob(p))
    for p in paths:
        run(p, tl)
