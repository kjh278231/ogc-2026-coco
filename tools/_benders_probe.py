"""Probe (1-1, Benders style): can a Gurobi assignment MIP + packing-feasibility cuts
beat our ILS on the TRUE objective? Iterate: solve (min w2*Z2 + w3*Z3, fits) -> pack each
bay (solve_bay) -> for bays with tardiness, add a no-good cut forbidding that bay's set ->
re-solve. Track the best TRUE obj (w1*Z1 + w2*Z2 + w3*Z3) seen. The naive (no-cut) MIP
packs infeasibly (Z1 explodes); this tests whether cuts find a packable good assignment.
Usage: python tools/_benders_probe.py <inst> [iters=40]"""
import os, sys, json, time
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_UNIFIED_ILS", "1"), ("SOLVER_UNIFIED_INIT_FRAC", "0.6"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
os.environ["SOLVER_MAX_EVALS"] = "2500"
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
inst = sys.argv[1]
iters = int(sys.argv[2]) if len(sys.argv) > 2 else 40
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "submission"))
import solver
import gurobipy as gp
from gurobipy import GRB

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
blocks, bays, w = prob["blocks"], prob["bays"], prob["weights"]
n, m = len(blocks), len(bays)


def true_obj(asg):
    tot, perbay = solver.total_obj(prob, asg, {})
    z2, z3 = solver.obj23(prob, asg)
    return tot, sum(perbay.values()), z2, z3, perbay


# ILS reference
best_ils, _ = solver.framework_solve(prob, 10 ** 9, _return_assignment=True)
ils = true_obj(best_ils)

# Benders MIP
areas = [b["width"] * b["height"] for b in bays]
avg = sum(areas) / m
u = [avg / areas[j] for j in range(m)]
md = gp.Model(env=solver._grb_env())
x = [[md.addVar(vtype=GRB.BINARY) for j in range(m)] for i in range(n)]
for i in range(n):
    md.addConstr(gp.quicksum(x[i][j] for j in range(m)) == 1)
    for j in range(m):
        if not solver.fits(blocks[i], bays[j]):
            md.addConstr(x[i][j] == 0)
load = [gp.quicksum(blocks[i]["workload"] * x[i][j] for i in range(n)) for j in range(m)]
M = md.addVar(lb=0)
for a in range(m):
    for b in range(m):
        if a != b:
            md.addConstr(M >= u[a] * load[a] - u[b] * load[b])
pref = gp.quicksum((max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]) * x[i][j]
                   for i in range(n) for j in range(m))
md.setObjective(w["w2"] * M + w["w3"] * pref, GRB.MINIMIZE)
md.Params.TimeLimit = 5

t0 = time.time()
best = None
nfeas = 0
for it in range(iters):
    md.optimize()
    if md.SolCount == 0:
        break
    asg = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
    tot, z1, z2, z3, perbay = true_obj(asg)
    if best is None or tot < best[0]:
        best = (tot, z1, z2, z3, it)
    if z1 <= 1e-9:
        nfeas += 1
        break  # Z1=0 + min Z2+Z3 among Z1=0 -> optimal of this relaxation
    # cut every bay that has tardiness
    for j in range(m):
        if perbay[j] > 1e-9:
            S = [i for i in range(n) if asg[i] == j]
            md.addConstr(gp.quicksum(x[i][j] for i in S) <= len(S) - 1)
wall = time.time() - t0
out = {"inst": inst, "ils_obj": round(ils[0]), "ils_z1z2z3": [round(ils[1]), round(ils[2]), round(ils[3])],
       "benders_best_obj": round(best[0]) if best else None,
       "benders_z1z2z3": [round(best[1]), round(best[2]), round(best[3])] if best else None,
       "delta_%": round((best[0] - ils[0]) / ils[0] * 100, 2) if best and ils[0] else None,
       "best_at_iter": best[4] if best else None, "iters_run": it + 1, "wall_s": round(wall, 1)}
print("BEND " + json.dumps(out))
