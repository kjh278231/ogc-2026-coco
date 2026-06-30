"""cProfile the current BRIDGE search hot path at a FIXED eval budget (deterministic),
to find the post-numba/prepare/bbcache #1 self-time cost = the next pure-speed target.
Pure speed => same eval results faster => more evals in the wall => better objective
(the stack is eval-limited). Run NORECOMB to isolate the search loop.

usage: python _profile_speed.py <prob_json> <max_evals>
"""
import json, os, sys, cProfile, pstats, io

prob_path, max_evals = sys.argv[1], sys.argv[2]
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
import solver

prob = json.load(open(prob_path, encoding="utf-8"))
name = os.path.splitext(os.path.basename(prob_path))[0]

# warm the numba JIT once (exclude compile time from the profile)
solver.framework_solve(json.loads(json.dumps(prob)), 60)

pr = cProfile.Profile()
pr.enable()
sol = solver.framework_solve(prob, 60)
pr.disable()

st = pstats.Stats(pr, stream=sys.stdout)
print("=== %s E=%s : top 25 by SELF time (tottime) ===" % (name, max_evals))
st.sort_stats("tottime").print_stats(25)
print("=== top 15 by CUMULATIVE time ===")
st.sort_stats("cumulative").print_stats(15)
