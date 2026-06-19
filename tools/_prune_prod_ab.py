"""Production A/B for the cost-prune default (single-process path).
cost = SOLVER_POOL_PRUNE_COST=1 (new default), tard = =0 (legacy tardiness prune).
Sequential only (each run uses Gurobi for recombine; single-use license).
Usage: python tools/_prune_prod_ab.py <inst> <T> <cost|tard>
"""
import os, sys, json, time, multiprocessing as mp
if __name__ == "__main__":
    mp.freeze_support()
    inst, T, cfg = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    os.environ["SOLVER_PORTFOLIO"] = "0"
    os.environ["SOLVER_POOL_PRUNE_COST"] = "0" if cfg == "tard" else "1"
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(ROOT, "submission"))
    import myalgorithm, utils
    prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, timelimit=T)
    wall = time.time() - t0
    r = utils.check_feasibility(prob, sol)
    flag = "" if (r.get("feasible") and wall <= T + 1) else "  <<< PROBLEM"
    print("RESULT " + json.dumps({"inst": inst, "T": T, "cfg": cfg,
          "feasible": bool(r.get("feasible")), "obj": r.get("objective"),
          "wall": round(wall, 1), "over": wall > T + 1}) + flag)
