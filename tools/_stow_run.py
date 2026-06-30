"""Run STOW's entry point, true-scored. Spawn-safe (paths set at module level so the
re-imported child has them; the actual run is under __main__).
Usage: _stow_run.py <Tname> <wall_seconds>
"""
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# module-level (so multiprocessing 'spawn' children, which re-import this module, get the paths)
if str(ROOT / "stow") not in sys.path:
    sys.path.insert(0, str(ROOT / "stow"))
if str(ROOT / "bridge") not in sys.path:
    sys.path.append(str(ROOT / "bridge"))


def main():
    name = sys.argv[1]
    T = float(sys.argv[2])
    import myalgorithm            # stow's (path[0])
    from utils import check_feasibility  # bridge's
    prob = json.load(open(ROOT / "train" / f"{name}.json"))
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, T)
    wall = time.time() - t0
    chk = check_feasibility(prob, sol)
    try:
        import portfolio
        last = getattr(portfolio, "LAST", {})
    except Exception:
        last = {}
    print(f"STOW {name} T={T}: obj={chk.get('objective')} obj1={chk.get('obj1')} "
          f"obj3={chk.get('obj3')} feas={chk.get('feasible')} stage={chk.get('stage')} "
          f"wall={wall:.1f}  mode={last.get('mode')} nW={last.get('n_workers')}")


if __name__ == "__main__":
    main()
