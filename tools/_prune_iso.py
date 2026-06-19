"""Isolated, deterministic test of the pool-prune CRITERION on the base recombine.

Run the search ONCE (eval-count, fills the global piece pool + a pre-recombine incumbent),
snapshot the pool, then run _recombine on the SAME pool/incumbent twice -- pruned by tardiness
vs by full column cost (w1*T + w3*pref) -- and compare. Isolates the prune criterion from
search noise (the search is identical; only the recombine prune key differs).
Usage: python tools/_prune_iso.py <inst> <E>
"""
import os, sys, json, time
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_UNIFIED_ILS", "1"), ("SOLVER_UNIFIED_INIT_FRAC", "0.6"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
inst, E = sys.argv[1], sys.argv[2]
os.environ["SOLVER_MAX_EVALS"] = E
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
os.environ["SOLVER_MIP_REPAIR"] = "0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "submission"))
import solver

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
# search (internal recombine uses default tardiness prune; we re-recombine in isolation below)
best, base = solver.framework_solve(prob, 10 ** 9, _return_assignment=True)
pool_snap = dict(solver._POOL)
solver._EVAL_LIMIT = None      # leave eval mode so the isolated recombines run unmetered
solver._EVALS = 0


def true_tot(asg):
    tot, _ = solver.total_obj(prob, asg, {})
    return round(tot)


def recomb_with(cost_flag):
    solver._POOL.clear(); solver._POOL.update(pool_snap)
    os.environ["SOLVER_POOL_PRUNE_COST"] = cost_flag
    r = solver._recombine(prob, dict(base), deadline=time.time() + 30.0)
    return true_tot(r)


b = true_tot(base)
# Force the per-bay prune to ACTIVATE (production T=180 pools are large enough that it does;
# this E=1500 pool is small, so shrink per_bay below pool/m) and compare tardiness vs cost
# at several aggressive levels.
for per_bay in (150, 250, 350):
    os.environ["SOLVER_POOL_PER_BAY"] = str(per_bay)
    o_tard = recomb_with("0")
    o_cost = recomb_with("1")
    pct = (o_cost - o_tard) / o_tard * 100 if o_tard else 0.0
    print("PRUNE " + json.dumps({"inst": inst, "E": int(E), "pool": len(pool_snap),
          "per_bay": per_bay, "base": b, "tardiness": o_tard, "cost": o_cost,
          "cost_vs_tard_%": round(pct, 2)}), flush=True)
