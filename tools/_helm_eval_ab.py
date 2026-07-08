"""Eval-count A/B runner for HELM vs PRISM vs FLUX (deterministic obj; one engine+instance
per process to keep id()-keyed module caches clean -- see memory perf-ab-one-process-per-
instance / eval-count-ab-protocol).

    python tools/_helm_eval_ab.py <engine:prism|flux|helm> <inst> <E> [env KEY=V ...]

Prints one JSON line: engine, inst, E, obj (true packed obj via utils.check_feasibility),
z123, regime (helm only), wall_s.
"""
import os, sys, json, time


def main():
    engine, inst, E = sys.argv[1], sys.argv[2], sys.argv[3]
    for kv in sys.argv[4:]:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.environ["SOLVER_MAX_EVALS"] = str(E)
    # deployed Pareto-safe stack (same for all engines; A/B isolates the anchor routing)
    for k in ("SOLVER_MASK_SEARCH", "SOLVER_MASK", "SOLVER_NUMBA", "SOLVER_MASK_PREPARE",
              "SOLVER_MULTIORDER", "SOLVER_SWAP"):
        os.environ.setdefault(k, "1")
    prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
    sys.path.insert(0, os.path.join(ROOT, engine))
    t0 = time.time()
    regime = None
    if engine == "prism":
        import prism_engine as M
        sol = M.prism_solve(prob, 10**9)
        stats = dict(M.LAST_STATS)
    elif engine == "flux":
        import flux_engine as M
        sol = M.flux_solve(prob, 10**9)
        stats = dict(M.LAST_STATS)
    else:
        import helm_engine as M
        sol = M.helm_solve(prob, 10**9)
        stats = dict(M.LAST_STATS)
        regime = stats.get("regime")
    wall = time.time() - t0
    sys.path.insert(0, os.path.join(ROOT, "bridge"))
    import utils
    r = utils.check_feasibility(prob, sol)
    print(json.dumps({
        "engine": engine, "inst": inst, "E": int(E),
        "feasible": r["feasible"],
        "obj": round(r["objective"]) if r.get("objective") else None,
        "z123": [round(r["obj1"]), round(r["obj2"]), round(r["obj3"])] if r["feasible"] else None,
        "regime": regime, "best_anchor": stats.get("best_anchor"),
        "wall_s": round(wall, 1)}))


if __name__ == "__main__":
    main()
