"""One framework_solve run, env set BEFORE import, true-scored by check_feasibility.
Usage: _mo_run.py <Tname> <E_evals|wall:SECONDS> <multiorder 0/1>
Prints: RESULT <true_obj> <obj1> <obj3> <feasible> <stage> <wall_s>
"""
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
name, budget, mo = sys.argv[1], sys.argv[2], sys.argv[3]

# shipped search/build geometry defaults + numba speed; multiorder toggled by argv
os.environ["SOLVER_MASK"] = "1"
os.environ["SOLVER_MASK_SEARCH"] = "1"
os.environ["SOLVER_NUMBA"] = "1"
os.environ["SOLVER_UNIFIED_ILS"] = "1"
os.environ["SOLVER_LAHC"] = "1"
os.environ["SOLVER_MULTIORDER"] = mo
if budget.startswith("wall:"):
    T = float(budget.split(":")[1])
else:
    os.environ["SOLVER_MAX_EVALS"] = budget
    T = 9999.0   # eval-count mode ignores wall

sys.path.insert(0, str(ROOT / "bridge"))
import solver            # noqa: E402
from utils import check_feasibility  # noqa: E402

prob = json.load(open(ROOT / "train" / f"{name}.json"))
t0 = time.time()
sol = solver.framework_solve(prob, T)
wall = time.time() - t0
chk = check_feasibility(prob, sol)
print(f"RESULT {chk.get('objective')} {chk.get('obj1')} {chk.get('obj3')} "
      f"{chk.get('feasible')} {chk.get('stage')} {wall:.2f}")
