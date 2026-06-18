"""Probe: MIP-propose + ILS-repair. The Z2+Z3-optimal assignment has very low Z3 but
Z1>0 (catastrophic given huge w1). Can local_search REPAIR it to Z1=0 while keeping Z3
far below the ILS's Z3? If yes -> a low-Z3/Z1=0 basin the pure ILS misses -> step-change.
Reports ILS, raw MIP, repaired-MIP, and best-of(repaired, ILS).
Usage: python tools/_hybrid_probe.py <inst>"""
import os, sys, json
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_UNIFIED_ILS", "1"), ("SOLVER_UNIFIED_INIT_FRAC", "0.6"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
os.environ["SOLVER_MAX_EVALS"] = "2500"
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
inst = sys.argv[1]
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
    return [round(tot), round(sum(perbay.values())), round(z2), round(z3)]


best_ils, _ = solver.framework_solve(prob, 10 ** 9, _return_assignment=True)
ils = true_obj(best_ils)

# Z2+Z3-optimal assignment MIP (low Z3, likely Z1>0)
areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
u = [avg / areas[j] for j in range(m)]
md = gp.Model(env=solver._grb_env())
x = [[md.addVar(vtype=GRB.BINARY) for j in range(m)] for i in range(n)]
for i in range(n):
    md.addConstr(gp.quicksum(x[i][j] for j in range(m)) == 1)
    for j in range(m):
        if not solver.fits(blocks[i], bays[j]):
            md.addConstr(x[i][j] == 0)
load = [gp.quicksum(blocks[i]["workload"] * x[i][j] for i in range(n)) for j in range(m)]
Mv = md.addVar(lb=0)
for a in range(m):
    for b in range(m):
        if a != b:
            md.addConstr(Mv >= u[a] * load[a] - u[b] * load[b])
pref = gp.quicksum((max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]) * x[i][j]
                   for i in range(n) for j in range(m))
md.setObjective(w["w2"] * Mv + w["w3"] * pref, GRB.MINIMIZE)
md.Params.TimeLimit = 10
md.optimize()
A_mip = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
mip = true_obj(A_mip)

# repair via local_search (optimizes total_obj -> huge w1 forces it to kill Z1)
solver._EVAL_LIMIT = 4000
solver._EVALS = 0
A_rep, _ = solver.local_search(prob, dict(A_mip), {}, 4000)
rep = true_obj(A_rep)
bestof = min(rep[0], ils[0])
print("HYB " + json.dumps({"inst": inst, "ils": ils, "mip_raw": mip, "mip_repaired": rep,
      "repaired_vs_ils_%": round((rep[0] - ils[0]) / ils[0] * 100, 2) if ils[0] else None,
      "bestof_vs_ils_%": round((bestof - ils[0]) / ils[0] * 100, 2) if ils[0] else None}))
