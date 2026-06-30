"""A/B: PRISM (anchor-spectrum) vs BRIDGE (framework_solve), one algo per process.

Both run at a fixed eval budget (SOLVER_MAX_EVALS) -> deterministic objective, and are
scored on the IDENTICAL basis K._score_and_pack (the emitted objective). Wall time is
reported separately as cost. Usage:
    python tools/_prism_ab.py <bridge|prism> <inst> [evals=4000]
"""
import os, sys, json, time

algo = sys.argv[1]
inst = sys.argv[2]
evals = sys.argv[3] if len(sys.argv) > 3 else "4000"
os.environ["SOLVER_MAX_EVALS"] = evals
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
# Recombine MIP single-threaded -> deterministic (parallel Gurobi is not reproducible
# even with a fixed seed). Same for both algos = fair, and matches the portfolio workers.
os.environ["SOLVER_CP_WORKERS"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if algo == "bridge":
    # full deployed single-process BRIDGE config (eval-mode no-ops: adaptive/idle).
    for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
                 ("SOLVER_MASK_PREPARE", "1"), ("SOLVER_UNIFIED_ILS", "1"),
                 ("SOLVER_UNIFIED_INIT_FRAC", "0.6"), ("SOLVER_UNIFIED_INIT_CAP", "45"),
                 ("SOLVER_LAHC", "1"), ("SOLVER_LAHC_L", "1"), ("SOLVER_MIP_REPAIR", "1")):
        os.environ.setdefault(k, v)
    sys.path.insert(0, os.path.join(ROOT, "bridge"))
    import solver as K
    prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
    t0 = time.time()
    best, _ = K.framework_solve(prob, 10 ** 9, _return_assignment=True)
    wall = time.time() - t0
elif algo == "prism":
    for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
                 ("SOLVER_MASK_PREPARE", "1")):
        os.environ.setdefault(k, v)
    sys.path.insert(0, os.path.join(ROOT, "prism"))
    import prism_engine as P
    K = P.K
    prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
    t0 = time.time()
    best, _ = P.prism_solve(prob, 10 ** 9, _return_assignment=True)
    wall = time.time() - t0
else:
    print("unknown algo", algo); sys.exit(1)

w = prob["weights"]
tot, packed = K._score_and_pack(prob, best, poly_deadline=None)
z2, z3 = K.obj23(prob, best)
z1 = round((tot - w["w2"] * z2 - w["w3"] * z3) / w["w1"])
print(json.dumps({"algo": algo, "inst": inst, "obj": round(tot),
                  "z1z2z3": [z1, round(z2), round(z3)], "wall_s": round(wall, 1)}))
