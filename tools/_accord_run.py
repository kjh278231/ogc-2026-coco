"""Run ACCORD's entry point, true-scored (utils.check_feasibility -- never trust the
solver's own objective). Usage: _accord_run.py <Tname> <wall_seconds>
"""
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "accord") not in sys.path:
    sys.path.insert(0, str(ROOT / "accord"))
if str(ROOT / "bridge") not in sys.path:
    sys.path.append(str(ROOT / "bridge"))


def main():
    name = sys.argv[1]
    T = float(sys.argv[2])
    import myalgorithm            # accord's (path[0])
    from utils import check_feasibility  # bridge's
    prob = json.load(open(ROOT / "train" / f"{name}.json"))
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, T)
    wall = time.time() - t0
    chk = check_feasibility(prob, sol)
    try:
        import accord_engine
        st = dict(accord_engine.LAST_STATS)
        traj = st.pop("traj", [])
    except Exception:
        st, traj = {}, []
    print(f"ACCORD {name} T={T}: obj={chk.get('objective')} obj1={chk.get('obj1')} "
          f"obj3={chk.get('obj3')} feas={chk.get('feasible')} stage={chk.get('stage')} "
          f"wall={wall:.1f}  {st}")
    if traj:
        print(f"  traj({len(traj)}): {[t[0] for t in traj]}")


if __name__ == "__main__":
    main()
