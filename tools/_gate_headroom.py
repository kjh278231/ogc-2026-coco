"""Z2/Z3-headroom gate signals (deterministic, no search -> no wall noise).

Two candidate gate signals, vs the 6 known MIP-repair win/loss labels:
  Z2_apref : load imbalance of the preference-greedy assignment a_pref (NO MIP needed)
  GAP      : (S_pref - S_mip)/S_pref, surrogate (w2*Z2+w3*Z3) drop the MIP buys (needs MIP)
High value => more reassignment headroom the single-trajectory ILS misses => MIP should help.
Usage: python tools/_gate_headroom.py
"""
import json, glob, os, sys
sys.path.insert(0, "submission")
import solver
import gurobipy as gp
from gurobipy import GRB

known = {"prob_11": "W", "prob_13": "W", "prob_20": "W",
         "prob_37": "L", "prob_26": "L", "prob_40": "L"}


def mv_of(prob, asg):
    bays = prob["bays"]; m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
    u = [avg / areas[j] for j in range(m)]
    load = [0.0] * m
    for i, j in asg.items():
        load[j] += prob["blocks"][i]["workload"]
    return max(u[a] * load[a] - u[b] * load[b] for a in range(m) for b in range(m) if a != b)


def mip_solve(prob):
    blocks, bays, w = prob["blocks"], prob["bays"], prob["weights"]
    n, m = len(blocks), len(bays)
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
    A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
    return md.ObjVal, A


rows = []
for f in sorted(glob.glob("train/*.json"), key=lambda p: int(p.split("_")[1].split(".")[0])):
    prob = json.load(open(f, encoding="utf-8")); w = prob["weights"]
    name = os.path.basename(f).replace(".json", "")
    ap = solver.a_pref(prob)
    z2_ap, z3_ap = solver.obj23(prob, ap)
    Mv_ap = mv_of(prob, ap)
    S_pref = w["w2"] * Mv_ap
    S_mip, Amip = mip_solve(prob)
    z2_m, z3_m = solver.obj23(prob, Amip)
    gap = (S_pref - S_mip) / S_pref if S_pref > 1e-9 else 0.0
    rows.append((name, round(gap, 3), round(z2_ap), round(Mv_ap), round(S_pref),
                 round(S_mip), round(z3_m), known.get(name, "")))

rows.sort(key=lambda r: -r[1])
print(f"{'inst':8s} {'GAP':>6s} | {'Z2_apref':>9s} {'Mv_apref':>9s} {'S_pref':>9s} "
      f"{'S_mip':>9s} {'z3_mip':>7s} | L")
for r in rows:
    print(f"{r[0]:8s} {r[1]:6.2f} | {r[2]:9d} {r[3]:9d} {r[4]:9d} {r[5]:9d} {r[6]:7d} | {r[7]}")
