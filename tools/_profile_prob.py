"""cProfile a deterministic framework_solve to find the hot functions post scan-port.
Usage: python tools/_profile_prob.py <inst> [E=1000]"""
import cProfile, pstats, io, os, sys, json
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_UNIFIED_ILS", "1"), ("SOLVER_UNIFIED_INIT_FRAC", "0.6"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
inst = sys.argv[1]
E = sys.argv[2] if len(sys.argv) > 2 else "1000"
os.environ["SOLVER_MAX_EVALS"] = E
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "submission"))
import solver
prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
os.environ["SOLVER_MAX_EVALS"] = "50"; solver.framework_solve(prob, 10 ** 9)   # warm numba
os.environ["SOLVER_MAX_EVALS"] = E
pr = cProfile.Profile(); pr.enable()
solver.framework_solve(prob, 10 ** 9)
pr.disable()
s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(16)
print(f"===== {inst} E={E} top by tottime =====")
for line in s.getvalue().splitlines():
    if line.strip() and ("ncalls" in line or "/" in line or "{" in line or "solver.py" in line
                         or "shapely" in line or ".py:" in line):
        print(line)
