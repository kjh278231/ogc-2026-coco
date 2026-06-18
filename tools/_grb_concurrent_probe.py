"""Does this WLS license permit 2 concurrent Gurobi sessions (parent + spawned child)?
Each side builds a tiny model and optimizes while the other holds its env open."""
import os, sys, time, multiprocessing as mp
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "submission"))

def _child(q):
    try:
        import gurobipy as gp
        from gurobipy import GRB
        env = gp.Env(empty=True); env.setParam("OutputFlag", 0); env.start()
        m = gp.Model(env=env); x = m.addVar(); m.addConstr(x >= 3); m.setObjective(x, GRB.MINIMIZE)
        time.sleep(1.0)            # hold the session open to overlap the parent
        m.optimize()
        q.put(("child", "OK", m.ObjVal))
    except Exception as e:
        q.put(("child", "FAIL", repr(e)))

if __name__ == "__main__":
    mp.freeze_support()
    import gurobipy as gp
    from gurobipy import GRB
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(q,)); p.start()
    try:
        env = gp.Env(empty=True); env.setParam("OutputFlag", 0); env.start()
        m = gp.Model(env=env); x = m.addVar(); m.addConstr(x >= 5); m.setObjective(x, GRB.MINIMIZE)
        time.sleep(1.0)
        m.optimize()
        print("PARENT OK", m.ObjVal)
    except Exception as e:
        print("PARENT FAIL", repr(e))
    p.join(30)
    print("CHILD", q.get() if not q.empty() else "no-result")
