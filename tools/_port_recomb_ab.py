"""Portfolio recombine-fix A/B (single-use Gurobi license).

control  = current portfolio (all workers try recombine; n-1 fail 10009 + waste reserve)
combined = SOLVER_PORT_MASTER_RECOMB=1 (workers search-only + return pools; master does ONE
           recombine over the UNION of all worker pools)

Runs ONE instance / ONE cfg per process. MUST be invoked sequentially -- the Gurobi
license is machine-global, so two concurrent portfolios' workers would contend and
corrupt the measurement.
Usage: python tools/_port_recomb_ab.py <inst> <T> <control|combined>
"""
import os, sys, json, time, multiprocessing as mp

if __name__ == "__main__":
    mp.freeze_support()
    inst, T, cfg = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    os.environ["SOLVER_PORTFOLIO"] = "1"
    os.environ["SOLVER_PORT_MASTER_RECOMB"] = "1" if cfg == "combined" else "0"
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
