"""One PRISM (multi-order) run at eval-count, SOLVER_SWAP toggled, true-scored.
Env set before import. PRISM_ANCHOR_FULL_BUDGET models the portfolio (each anchor full E).
Usage: _prism_swap_run.py <Tname> <E> <swap 0/1>
Prints: RESULT swap=<> <obj> <obj1> <obj3> <feasible> <stage> <wall_s>
"""
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
name, E, swap = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["SOLVER_MASK"] = "1"
os.environ["SOLVER_MASK_SEARCH"] = "1"
os.environ["SOLVER_NUMBA"] = "1"
os.environ["SOLVER_MULTIORDER"] = "1"
os.environ["SOLVER_SWAP"] = swap
os.environ["SOLVER_MAX_EVALS"] = E
os.environ["PRISM_ANCHOR_FULL_BUDGET"] = "1"
sys.path.insert(0, str(ROOT / "prism"))
sys.path.append(str(ROOT / "bridge"))

import prism_engine as P            # noqa: E402
from utils import check_feasibility  # noqa: E402

prob = json.load(open(ROOT / "train" / f"{name}.json"))
t0 = time.time()
sol = P.prism_solve(prob, 9999.0)
wall = time.time() - t0
chk = check_feasibility(prob, sol)
print(f"RESULT swap={swap} {chk.get('objective')} {chk.get('obj1')} {chk.get('obj3')} "
      f"{chk.get('feasible')} {chk.get('stage')} {wall:.1f}")
