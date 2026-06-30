"""Wall-clock A/B of the deployed portfolios via the real algorithm() entry point.

    python tools/_prism_portf_ab.py <prism|bridge> <inst> <timelimit>

__main__-guarded so multiprocessing 'spawn' is safe (the portfolios require it). Scores
the EMITTED solution with the grader's utils.check_feasibility (the true objective, incl.
any feasibility/overrun failure). One algo per process.
"""
import os, sys, json, time


def main():
    algo, inst, T = sys.argv[1], sys.argv[2], float(sys.argv[3])
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
    if algo == "prism":
        sys.path.insert(0, os.path.join(ROOT, "prism"))
    elif algo == "stow":
        sys.path.insert(0, os.path.join(ROOT, "stow"))
    else:
        sys.path.insert(0, os.path.join(ROOT, "bridge"))
    import myalgorithm as M
    t0 = time.time()
    sol = M.algorithm(prob, T)
    wall = time.time() - t0
    last = {}
    if algo == "prism":
        try:
            import portfolio
            last = dict(portfolio.LAST)
        except Exception:
            pass
    sys.path.insert(0, os.path.join(ROOT, "bridge"))
    import utils
    r = utils.check_feasibility(prob, sol)
    if last:
        sys.stderr.write("LAST " + json.dumps(last) + "\n")
    print(json.dumps({
        "algo": algo, "inst": inst, "T": T, "feasible": r["feasible"], "stage": r["stage"],
        "obj": round(r["objective"]) if r.get("objective") else None,
        "obj123": [round(r["obj1"]), round(r["obj2"]), round(r["obj3"])] if r["feasible"] else None,
        "wall_s": round(wall, 1)}))


if __name__ == "__main__":
    main()
