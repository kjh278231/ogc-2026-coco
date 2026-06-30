"""Probe (FACET): assignment MIP master + SUPERSET-AWARE SOFT-PENALTY packing cuts.

Improves on _benders_probe.py's weak no-good cut (Sum x <= |S|-1, which whack-a-moles
and never drives Z1->0). Instead of FORBIDDING a tardy set, we PRICE its tardiness:
for a tardy bay j with block-set S and tardiness T, add
    y >= Sum_{i in S} x[i][j] - (|S|-1)   (y in [0,1], =1 iff ALL of S assigned to j)
    objective += w1 * T * y
By monotonicity (a superset of a tardy set is >= as tardy), y also fires for any
SUPERSET of S in j, so pricing S charges a valid LOWER BOUND on every super-crowding.
The master objective becomes an under-estimator of the true objective that tightens
each iteration (logic-based Benders / integer L-shaped). We keep the best TRUE-scored
assignment regardless, so even imperfect cuts are safe.

Usage: python tools/_facet_probe.py <inst> [iters=40]
"""
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


# ILS reference (eval-count fixed -> deterministic)
best_ils, _ = solver.framework_solve(prob, 10 ** 9, _return_assignment=True)
ils = true_obj(best_ils)

# Master MIP base: min w2*Z2 + w3*Z3, fits-constrained.
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
base_obj = w["w2"] * M + w["w3"] * pref
penalty_terms = []          # list of (coef, yvar)
md.Params.TimeLimit = 5
md.Params.OutputFlag = 0

t0 = time.time()
best = None
seen_cuts = set()           # (j, frozenset(S)) already priced
last_asg = None
converged = ""
for it in range(iters):
    md.setObjective(base_obj + gp.quicksum(c * y for c, y in penalty_terms), GRB.MINIMIZE)
    md.optimize()
    if md.SolCount == 0:
        converged = "no_sol"
        break
    asg = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
    tot, z1, z2, z3, perbay = true_obj(asg)
    if best is None or tot < best[0]:
        best = (tot, z1, z2, z3, it)
    if z1 <= 1e-9:
        converged = "z1_zero"
        break
    fa = tuple(sorted(asg.items()))
    if fa == last_asg:
        # master re-proposed the same assignment though its tardy sets are priced
        # => optimal w.r.t. the (exact-at-this-point) model. Stop.
        converged = "fixpoint"
        break
    last_asg = fa
    new = 0
    for j in range(m):
        if perbay[j] > 1e-9:
            S = frozenset(i for i in range(n) if asg[i] == j)
            key = (j, S)
            if key in seen_cuts:
                continue
            seen_cuts.add(key)
            y = md.addVar(lb=0.0, ub=1.0)
            md.addConstr(y >= gp.quicksum(x[i][j] for i in S) - (len(S) - 1))
            penalty_terms.append((w["w1"] * perbay[j], y))
            new += 1
    if new == 0:
        converged = "no_new_cut"
        break
wall = time.time() - t0
out = {"inst": inst, "ils_obj": round(ils[0]), "ils_z1z2z3": [round(ils[1]), round(ils[2]), round(ils[3])],
       "facet_best_obj": round(best[0]) if best else None,
       "facet_z1z2z3": [round(best[1]), round(best[2]), round(best[3])] if best else None,
       "delta_%": round((best[0] - ils[0]) / ils[0] * 100, 2) if best and ils[0] else None,
       "best_at_iter": best[4] if best else None, "iters_run": it + 1,
       "cuts": len(seen_cuts), "stop": converged, "wall_s": round(wall, 1)}
print("FACET " + json.dumps(out))
