"""Why does the cross-worker UNION recombine underperform the per-worker recombine (prob_6)?
Hypotheses: (A) the improving pieces get pruned out of the union; (B) the pieces are present
but the bigger/harder union MIP can't FIND the cover in the time budget.

Runs the 4 portfolio seeds (deterministic eval-count), captures each worker's pre-recombine
incumbent + post-recombine best + pool. Then: (1) the best per-worker recombine obj A* and
whether A*'s pieces are all present in the UNION pool (representability); (2) the union
recombine seeded with the best SEARCH incumbent at increasing solve budgets (replicating the
non-additive failure, then giving it more time / no NoRel) -- if it falls toward A* with more
time, it was time-limited (B); if it stays high, the union genuinely can't represent/reach A* (A).
Usage: python tools/_union_diag.py <inst>
"""
import os, sys, json, time
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_UNIFIED_ILS", "1"), ("SOLVER_UNIFIED_INIT_FRAC", "0.6"),
             ("SOLVER_MASK_PREPARE", "1")):
    os.environ.setdefault(k, v)
inst = sys.argv[1] if len(sys.argv) > 1 else "prob_6"
os.environ["SOLVER_MAX_EVALS"] = sys.argv[2] if len(sys.argv) > 2 else "1500"
os.environ["SOLVER_PORTFOLIO"] = "0"
os.environ["SOLVER_IDLE_ILS"] = "0"
os.environ["SOLVER_MIP_REPAIR"] = "0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "submission"))
import solver

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
m = len(prob["bays"])
SEEDS = ["12346", "20265", "28184", "36103"]


def tot(a):
    return round(solver.total_obj(prob, a, {})[0])


def pieces_of(asg):
    return set((j, tuple(sorted(i for i in asg if asg[i] == j)))
               for j in range(m) if any(asg[i] == j for i in asg))


workers = []
for s in SEEDS:
    os.environ["SOLVER_SEED"] = s
    best, base = solver.framework_solve(prob, 10 ** 9, _return_assignment=True)  # search + recombine
    workers.append({"base": base, "best": best, "pool": dict(solver._POOL)})

solver._EVAL_LIMIT = None
solver._EVALS = 0

# best per-worker recombine result A*, and best pre-recombine search incumbent
pw = sorted(workers, key=lambda w: tot(w["best"]))
A_star, A_star_obj = pw[0]["best"], tot(pw[0]["best"])
search_best = min((w["base"] for w in workers), key=tot)
search_best_obj = tot(search_best)

# union pool + representability of A* in the union
union = {}
for w in workers:
    union.update(w["pool"])
ps = pieces_of(A_star)
present = sum(1 for p in ps if p in union)


def recomb(pool, A, solve_s, norel, per_bay="0"):
    solver._POOL.clear(); solver._POOL.update(pool)
    os.environ["SOLVER_RECOMB_SOLVE_S"] = str(solve_s)
    os.environ["SOLVER_RECOMB_NOREL"] = str(norel)
    os.environ["SOLVER_POOL_PER_BAY"] = per_bay
    r = solver._recombine(prob, dict(A), deadline=time.time() + solve_s + 60)
    return tot(r)


# union recombine from the SEARCH incumbent at rising budgets (the non-additive scenario)
u20 = recomb(union, search_best, 20, 10)
u60 = recomb(union, search_best, 60, 0)
u120 = recomb(union, search_best, 120, 0)
# control: per-worker-style recombine of A*'s own worker pool from its search incumbent
own = pw[0]["pool"]
own_recomb = recomb(own, pw[0]["base"], 8, 0)

print("UDIAG " + json.dumps({
    "inst": inst, "union_size": len(union), "A_star_obj": A_star_obj,
    "A_star_pieces": len(ps), "present_in_union": present,
    "representable": present == len(ps),
    "search_best": search_best_obj, "own_pool_recomb": own_recomb,
    "union_20s_norel": u20, "union_60s": u60, "union_120s": u120}))
