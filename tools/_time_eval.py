"""Fixed-eval wall timing for a given code dir (old shipped vs new bridge), to measure the
pure-speed win of the mask-marshal refactor. Same eval count => same work => wall delta is
the speedup. Warms up the numba JIT once (excluded), then times N timed runs; reports min
(most stable) + all.

usage: python _time_eval.py <code_dir> <prob_json> <max_evals> <tag> [n_timed]
"""
import json, os, sys, time

code_dir, prob_path, max_evals, tag = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
n_timed = int(sys.argv[5]) if len(sys.argv) > 5 else 3

os.environ["SOLVER_MASK_SEARCH"] = "1"
os.environ["SOLVER_MASK"] = "1"
os.environ["SOLVER_NUMBA"] = "1"
os.environ["SOLVER_UNIFIED_ILS"] = "1"
os.environ["SOLVER_MASK_PREPARE"] = "1"
os.environ["SOLVER_LAHC"] = "1"
os.environ["SOLVER_LAHC_L"] = "1"
os.environ["SOLVER_MAX_EVALS"] = max_evals
os.environ["SOLVER_NORECOMB"] = "1"
os.environ["SOLVER_PORTFOLIO"] = "0"
for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMBA_NUM_THREADS"):
    os.environ[k] = "1"

sys.path.insert(0, os.path.abspath(code_dir))
import solver, utils

prob = json.load(open(prob_path, encoding="utf-8"))
name = os.path.splitext(os.path.basename(prob_path))[0]

# warm up numba JIT (compile excluded from timing)
solver.framework_solve(json.loads(json.dumps(prob)), 60)

times = []
obj = None
for _ in range(n_timed):
    t0 = time.time()
    sol = solver.framework_solve(json.loads(json.dumps(prob)), 60)
    times.append(time.time() - t0)
    obj = utils.check_feasibility(prob, sol).get("objective", -1)

print("TIME %s %s obj=%.0f E=%s min=%.2fs all=%s" % (
    name, tag, obj, max_evals, min(times),
    ",".join("%.2f" % t for t in times)), flush=True)
