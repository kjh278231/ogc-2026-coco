"""Run ANY solver folder's myalgorithm entry at a wall budget, true-scored. Spawn-safe
(paths at module level; run under __main__ so portfolio spawn re-imports work).
Usage: _entry_run.py <solverdir e.g. prism|accord|stow> <Tname> <wall_seconds>
"""
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOLVER_DIR = sys.argv[1]
if str(ROOT / SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT / SOLVER_DIR))
if str(ROOT / "bridge") not in sys.path:
    sys.path.append(str(ROOT / "bridge"))


def main():
    name = sys.argv[2]
    T = float(sys.argv[3])
    import myalgorithm
    from utils import check_feasibility
    prob = json.load(open(ROOT / "train" / f"{name}.json"))
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, T)
    wall = time.time() - t0
    chk = check_feasibility(prob, sol)
    print(f"{SOLVER_DIR.upper()} {name} T={T}: obj={chk.get('objective')} "
          f"obj1={chk.get('obj1')} obj3={chk.get('obj3')} feas={chk.get('feasible')} "
          f"stage={chk.get('stage')} wall={wall:.1f}")


if __name__ == "__main__":
    main()
