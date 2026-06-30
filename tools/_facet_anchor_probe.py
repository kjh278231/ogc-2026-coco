"""Probe (FACET v3): is the global min-Z3 MIP a BETTER SEED for LAHC than a_pref?

Falsifications so far: a global assignment MIP finds 5-30x lower Z3 than ILS but
catastrophic Z1, and neither global Benders cuts nor MIP-per-move LNS can fix Z1
cheaply. The surviving asset is the MIP's Z3 power. Cheapest way to harness it:
compute a* = argmin (w2*Z2 + w3*Z3) ONCE, then run the validated cheap-move LAHC
descent FROM a* (instead of from a_pref). If a*-seeded LAHC reaches a lower TRUE
objective at the same eval budget, "LAHC anchored at the preference-ideal" is a
viable distinct solver. Eval-count fixed -> deterministic.

Usage: python tools/_facet_anchor_probe.py <inst> [evals=4000]
"""
import os, sys, json
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
os.environ["SOLVER_PORTFOLIO"] = "0"
inst = sys.argv[1]
evals = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import solver
import gurobipy as gp
from gurobipy import GRB

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
blocks, bays, w = prob["blocks"], prob["bays"], prob["weights"]
n, m = len(blocks), len(bays)


def true_obj(asg):
    z2, z3 = solver.obj23(prob, asg)
    tot, perbay = solver.total_obj(prob, asg, {})
    return tot, sum(perbay.values()), z2, z3


def mip_anchor():
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / areas[j] for j in range(m)]
    md = gp.Model(env=solver._grb_env())
    md.Params.OutputFlag = 0
    md.Params.TimeLimit = 8
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
    md.optimize()
    A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
    md.dispose()
    return A


def lahc_from(seed, label):
    solver._EVALS = 0
    solver._EVAL_LIMIT = evals
    cache = {}
    A, tot = solver._climb_lahc(prob, dict(seed), cache, evals, 1)
    solver._EVAL_LIMIT = None
    return true_obj(A)


a_pref = solver.a_pref(prob)
a_star = mip_anchor()
r_pref = lahc_from(a_pref, "pref")
r_star = lahc_from(a_star, "star")
out = {"inst": inst, "evals": evals,
       "lahc_from_pref": [round(r_pref[0]), [round(r_pref[1]), round(r_pref[2]), round(r_pref[3])]],
       "lahc_from_star": [round(r_star[0]), [round(r_star[1]), round(r_star[2]), round(r_star[3])]],
       "delta_%": round((r_star[0] - r_pref[0]) / r_pref[0] * 100, 2) if r_pref[0] else None}
print("ANCH " + json.dumps(out))
