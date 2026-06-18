"""Gate-falsification probe (ONE instance per process -> clean caches).

Deterministic (eval-count, no wall noise). For one instance computes:
  cheap SIGNALS (all available before the expensive repair):
    m           = #bays
    util        = sum(block area) / sum(bay area)
    z2_apref    = Z2 load-imbalance of a_pref (Z3(a_pref)=0 by construction)
    gap         = (S_pref - S_mip)/S_pref, surrogate (w2*Z2+w3*Z3) drop the MIP buys
  LABEL (does MIP-repair actually beat the ILS at this eval budget E):
    obj_ils, obj_mip_rep, win_pct = (min(rep,ils) - ils)/ils  (<0 => MIP helps)
Usage: python tools/_gate_probe.py <inst> <E>
"""
import os, sys, json
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_UNIFIED_ILS", "1"), ("SOLVER_UNIFIED_INIT_FRAC", "0.6"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
inst, E = sys.argv[1], int(sys.argv[2])
os.environ["SOLVER_MAX_EVALS"] = str(E)
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
os.environ["SOLVER_MIP_REPAIR"] = "0"     # we drive the MIP-repair by hand below
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "submission"))
import solver
import gurobipy as gp
from gurobipy import GRB

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
blocks, bays, w = prob["blocks"], prob["bays"], prob["weights"]
n, m = len(blocks), len(bays)

# ---- cheap signals -------------------------------------------------------- #
def _poly_area(pts):
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0

# block footprint = orientation-0 first layer polygon (shoelace); bays are axis rectangles.
blk_area = sum(_poly_area(b["shape"][0]["layers"][0]) for b in blocks)
bay_area = sum(b["width"] * b["height"] for b in bays)
util = blk_area / bay_area
a_pref = solver.a_pref(prob)
z2_ap, z3_ap = solver.obj23(prob, a_pref)
S_pref = w["w2"] * z2_ap + w["w3"] * z3_ap            # z3_ap is ~0 (a_pref maximizes pref)

# ---- Z2+Z3 assignment MIP (mirror solver._mip_repair) --------------------- #
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
md.Params.TimeLimit = 5
md.optimize()
S_mip = md.ObjVal
A_mip = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
gap = (S_pref - S_mip) / S_pref if S_pref > 0 else 0.0

# ---- labels (deterministic, eval-count) ----------------------------------- #
def true_tot(asg):
    tot, _ = solver.total_obj(prob, asg, {})
    return round(tot)

best_ils, _ = solver.framework_solve(prob, 10 ** 9, _return_assignment=True)
obj_ils = true_tot(best_ils)
solver._EVAL_LIMIT = 3000
solver._EVALS = 0
A_rep, _ = solver.local_search(prob, dict(A_mip), {}, 3000)
obj_rep = true_tot(A_rep)
bestof = min(obj_rep, obj_ils)
win_pct = round((bestof - obj_ils) / obj_ils * 100, 2) if obj_ils else None

print("GATE " + json.dumps({
    "inst": inst, "E": E, "n": n, "m": m,
    "util": round(util, 3), "z2_apref": round(z2_ap),
    "gap": round(gap, 4), "obj_ils": obj_ils, "obj_rep": obj_rep,
    "win_pct": win_pct, "mip_wins": bool(obj_rep < obj_ils - 1e-9)}))
