"""Probe (FACET v2): MIP-LNS / fix-and-optimize with LOCAL packing-Benders.

Lesson from _facet_probe.py: a GLOBAL assignment MIP finds 30x lower Z3 than ILS but
catastrophic Z1, and set-cuts can't drive global Z1->0 (too many crowding patterns).
FIX: scope the MIP to a few bays. Start from a Z1~0 assignment; repeatedly free the
blocks of K bays and re-assign them OPTIMALLY among those K bays (full local objective:
w3*pref + global w2*Z2), holding all other bays fixed at Z1=0. Packing feasibility of
the K touched bays is enforced by a LOCAL Benders loop (soft-penalty cuts), which now
converges because only a few blocks/bays vary. Keep the move only if the TRUE objective
improves. This harnesses the MIP's proven Z3 power within a packing-checkable scope.

Usage: python tools/_facet_lns_probe.py <inst> [moves=120] [K=3] [seed=0]
"""
import os, sys, json, time, random
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_UNIFIED_ILS", "1"), ("SOLVER_UNIFIED_INIT_FRAC", "0.6"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
# ILS reference uses a fixed eval budget (deterministic); FACET uses a wall budget below.
ILS_EVALS = os.environ.get("FACET_ILS_EVALS", "2500")
inst = sys.argv[1]
moves = int(sys.argv[2]) if len(sys.argv) > 2 else 120
K = int(sys.argv[3]) if len(sys.argv) > 3 else 3
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "submission"))
import solver
import gurobipy as gp
from gurobipy import GRB

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
blocks, bays, w = prob["blocks"], prob["bays"], prob["weights"]
n, m = len(blocks), len(bays)
W1, W2, W3 = w["w1"], w["w2"], w["w3"]
areas = [b["width"] * b["height"] for b in bays]
avg = sum(areas) / m
u = [avg / areas[j] for j in range(m)]
prefmax = [max(b["bay_preferences"]) for b in blocks]
fit = [[solver.fits(blocks[i], bays[j]) for j in range(m)] for i in range(n)]


def true_obj(asg, cache):
    tot, perbay = solver.total_obj(prob, asg, cache)
    return tot, perbay


# ---- ILS reference (deterministic, fixed evals) ----
os.environ["SOLVER_MAX_EVALS"] = ILS_EVALS
solver._EVAL_LIMIT = int(ILS_EVALS)
solver._EVALS = 0
best_ils, _ = solver.framework_solve(prob, 10 ** 9, _return_assignment=True)
ils_tot, _ = true_obj(best_ils, {})

# ---- FACET MIP-LNS (wall-budgeted) ----
# turn OFF eval-limit so packer runs normally under a wall deadline
solver._EVAL_LIMIT = None
os.environ.pop("SOLVER_MAX_EVALS", None)
rng = random.Random(seed)
cache = {}

# start from a_pref then a short local_search to reach Z1~0 (independent of ILS result)
A = solver.a_pref(prob)
A, _ = solver.local_search(prob, A, cache, time.time() + 5.0)
best = dict(A)
best_tot, best_perbay = true_obj(best, cache)


def bay_set(asg, j):
    return [i for i in range(n) if asg[i] == j]


def local_mip_move(asg, Ks):
    """Re-assign blocks currently in bays Ks among Ks, min full local objective with
    local packing-Benders. Returns the best true-scored assignment found (>= incumbent)."""
    freed = [i for i in range(n) if asg[i] in Ks]
    if not freed:
        return None
    # fixed loads from non-Ks bays
    fixed_load = [0.0] * m
    for i in range(n):
        if asg[i] not in Ks:
            fixed_load[asg[i]] += blocks[i]["workload"]
    md = gp.Model(env=solver._grb_env())
    md.Params.OutputFlag = 0
    md.Params.TimeLimit = 2.0
    x = {(i, j): md.addVar(vtype=GRB.BINARY) for i in freed for j in Ks if fit[i][j]}
    for i in freed:
        cols = [x[i, j] for j in Ks if (i, j) in x]
        if not cols:
            md.dispose()
            return None
        md.addConstr(gp.quicksum(cols) == 1)
    load = {}
    for j in Ks:
        load[j] = fixed_load[j] + gp.quicksum(blocks[i]["workload"] * x[i, j]
                                              for i in freed if (i, j) in x)
    full_load = {j: (load[j] if j in Ks else fixed_load[j]) for j in range(m)}
    Mv = md.addVar(lb=0)
    for a in range(m):
        for b in range(m):
            if a != b:
                md.addConstr(Mv >= u[a] * full_load[a] - u[b] * full_load[b])
    pref = gp.quicksum((prefmax[i] - blocks[i]["bay_preferences"][j]) * x[i, j]
                       for i in freed for j in Ks if (i, j) in x)
    base = W2 * Mv + W3 * pref
    penalties = []
    seen = set()
    local_best = None
    for _ in range(6):
        md.setObjective(base + gp.quicksum(c * y for c, y in penalties), GRB.MINIMIZE)
        md.optimize()
        if md.SolCount == 0:
            break
        trial = dict(asg)
        for i in freed:
            for j in Ks:
                if (i, j) in x and x[i, j].X > 0.5:
                    trial[i] = j
                    break
        tot, perbay = true_obj(trial, cache)
        if local_best is None or tot < local_best[0]:
            local_best = (tot, trial)
        tardy_touch = [j for j in Ks if perbay.get(j, 0) > 1e-9]
        if not tardy_touch:
            break
        added = 0
        for j in tardy_touch:
            S = frozenset(i for i in freed if trial[i] == j)
            if (j, S) in seen or not S:
                continue
            seen.add((j, S))
            y = md.addVar(lb=0.0, ub=1.0)
            md.addConstr(y >= gp.quicksum(x[i, j] for i in S if (i, j) in x) - (len(S) - 1))
            penalties.append((W1 * perbay[j], y))
            added += 1
        if added == 0:
            break
    md.dispose()
    return local_best


t0 = time.time()
deadline = t0 + 30.0
applied = 0
mv = 0
# rank bays by Z3 regret of their blocks (hotspots to re-optimize)
while mv < moves and time.time() < deadline:
    mv += 1
    # choose K bays: a high-regret block's current bay + its preferred bay + randoms
    i0 = rng.randrange(n)
    cur = best[i0]
    pj = max((j for j in range(m) if fit[i0][j]),
             key=lambda j: blocks[i0]["bay_preferences"][j], default=cur)
    Ks = {cur, pj}
    while len(Ks) < min(K, m):
        Ks.add(rng.randrange(m))
    Ks = sorted(Ks)
    res = local_mip_move(best, Ks)
    if res and res[0] < best_tot - 1e-9:
        best_tot, best = res[0], dict(res[1])
        applied += 1
wall = time.time() - t0
solver._EVAL_LIMIT = None
out = {"inst": inst, "ils_obj": round(ils_tot), "facet_lns_obj": round(best_tot),
       "delta_%": round((best_tot - ils_tot) / ils_tot * 100, 2) if ils_tot else None,
       "moves_tried": mv, "moves_applied": applied, "wall_s": round(wall, 1)}
print("FLNS " + json.dumps(out))
