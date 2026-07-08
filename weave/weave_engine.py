"""WEAVE -- a fourth OGC 2026 solver: a co-evolving POPULATION on assignment vectors.

Paradigm (distinct from the other three):
  * BRIDGE = single-trajectory ILS/LAHC(relocate) + swap + MIP-repair + column-recombine.
  * PRISM  = MIP anchor spectrum -> each refined INDEPENDENTLY by LAHC -> best-of + column-recombine.
  * STOW   = packing-policy diversity portfolio.
  * WEAVE  = a POPULATION of diverse Z1=0 elites that exchange assignment STRUCTURE DURING
             search via crossover + path-relinking, plus an ejection-chain local search
             (k-opt generalisation of swap). Column-recombine only ASSEMBLES seen bay-pieces;
             assignment-level recombination + chains reach partitions it cannot construct.

Why this shape (measured, docs/newsolver_experiment_design.md + .claude/scratch/_recomb_probe2):
  * the dominant cost w3*Z3 + w2*Z2 is a PURE function of the partition; Z1~0 at good assigns;
  * blind crossover jumps off the thin feasible manifold (Z1 blows up) -> guarded polish/eject;
  * pair best x MAX-HAMMING partner (diversity is the recombination material): T18 -7.8% beat ILS;
  * uniform > greedy crossover; ejection chains traverse the manifold that relocation+swap can't.

Reuses ONLY the validated per-bay packing/scoring/recombine/guard primitives from BRIDGE (as K).
Time-managed; always returns a feasible solution. Env knobs: WEAVE_* (below) + the SOLVER_* stack.
"""
from __future__ import annotations
import os
import sys
import time
import random

_BRIDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE_DIR not in sys.path:
    sys.path.append(_BRIDGE_DIR)
import solver as K          # noqa: E402  validated kernel
import weave_ops as W       # noqa: E402  operators (import after K so its append is a no-op)

_gp = getattr(K, "_gp", None)
_GRB = getattr(K, "_GRB", None)
_HAS_GUROBI = getattr(K, "_HAS_GUROBI", False)

LAST_STATS: dict = {}


def _flag(name, default=False):
    return K._env_flag(name) if os.environ.get(name) is not None else default


# --------------------------------------------------------------------------- #
# MIP preference-ideal anchor (replicated from PRISM for self-containment)
# --------------------------------------------------------------------------- #
def mip_anchor(prob, lam, time_limit):
    """argmin (lam*w2*Z2 + w3*Z3) over block->bay (fits-constrained, Z2 min-max linearized).
    The preference ideal at balance pressure `lam`; ignores packing (Z1). Threads=1/Seed=0 for
    determinism. Returns an assignment dict or None."""
    if not _HAS_GUROBI:
        return None
    blocks = prob["blocks"]
    bays = prob["bays"]
    w = prob["weights"]
    n, m = len(blocks), len(bays)
    try:
        areas = [b["width"] * b["height"] for b in bays]
        avg = sum(areas) / m
        u = [avg / areas[j] for j in range(m)]
        md = _gp.Model(env=K._grb_env())
        md.Params.OutputFlag = 0
        x = [[md.addVar(vtype=_GRB.BINARY) for j in range(m)] for i in range(n)]
        for i in range(n):
            md.addConstr(_gp.quicksum(x[i][j] for j in range(m)) == 1)
            for j in range(m):
                if not K.fits(blocks[i], bays[j]):
                    md.addConstr(x[i][j] == 0)
        load = [_gp.quicksum(blocks[i]["workload"] * x[i][j] for i in range(n)) for j in range(m)]
        Mv = md.addVar(lb=0)
        for a in range(m):
            for b in range(m):
                if a != b:
                    md.addConstr(Mv >= u[a] * load[a] - u[b] * load[b])
        pref = _gp.quicksum(
            (max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]) * x[i][j]
            for i in range(n) for j in range(m))
        md.setObjective(lam * w["w2"] * Mv + w["w3"] * pref, _GRB.MINIMIZE)
        md.Params.TimeLimit = max(0.5, time_limit)
        md.Params.Threads = int(os.environ.get("WEAVE_MIP_THREADS", "1"))
        md.Params.Seed = 0
        md.optimize()
        if md.SolCount == 0:
            md.dispose()
            return None
        A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
        md.dispose()
        return A
    except Exception:
        return None


_HEUR = {"pref": "a_pref", "balanced": "a_balanced_load", "capped": "a_pref_capped"}


def _lambdas():
    raw = os.environ.get("WEAVE_LAMBDAS", "1,16")
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(float(tok))
            except ValueError:
                pass
    return out or [1.0, 16.0]


def _anchors(prob, mip_tl, want_mip=True):
    heur = os.environ.get("WEAVE_HEUR_ANCHORS", "pref,balanced,capped")
    anchors = []
    for name in (t.strip() for t in heur.split(",")):
        fn = _HEUR.get(name)
        if fn:
            anchors.append((name, getattr(K, fn)(prob)))
    if not anchors:
        anchors.append(("pref", K.a_pref(prob)))
    if want_mip and _HAS_GUROBI:
        for lam in _lambdas():
            A = mip_anchor(prob, lam, mip_tl)
            if A is not None:
                anchors.append(("mip%g" % lam, A))
    return anchors


# --------------------------------------------------------------------------- #
# refine one seed into a Z1=0 elite (LAHC + guided swap)
# --------------------------------------------------------------------------- #
def _refine(prob, seed, cache, dl, L, rng=None):
    best, bt = K._climb_lahc(prob, seed, cache, dl, L, rng=rng)
    if K._env_flag("SOLVER_SWAP"):
        best, bt = K._z3_refine(prob, best, cache, dl)
    return best, bt


def _polish(prob, asg, cache, dl, L, rng=None):
    """Local improvement of a child: LAHC descent + guided swap + optional ejection chains."""
    c, ct = K._climb_lahc(prob, asg, cache, dl, L, rng=rng)
    if K._env_flag("SOLVER_SWAP"):
        c, ct = K._z3_refine(prob, c, cache, dl)
    if _flag("WEAVE_EJECTION", True):
        c, ct = W.ejection_chain_search(prob, c, cache, dl,
                                        max_len=int(os.environ.get("WEAVE_EJECT_LEN", "6")))
    return c, ct


# --------------------------------------------------------------------------- #
# population helpers
# --------------------------------------------------------------------------- #
def _select(pop, rng):
    """Binary tournament by objective (selection pressure toward good parents)."""
    a = rng.choice(pop)
    b = rng.choice(pop)
    return a if a[2] <= b[2] else b


def _insert(pop, child, ct, maxpop):
    """Restricted-tournament replacement: the child competes with its most-similar member
    (min Hamming); replace it iff the child is strictly better. Preserves diversity (the
    recombination material) instead of collapsing the population onto near-duplicates."""
    if len(pop) < maxpop:
        pop.append(["child", child, ct])
        return True
    closest = min(pop, key=lambda e: W.hamming(child, e[1]))
    if ct < closest[2] - 1e-9:
        closest[1], closest[2] = child, ct
        closest[0] = "child"
        return True
    return False


# --------------------------------------------------------------------------- #
# reusable population build + evolve (shared by weave_solve and the portfolio worker)
# --------------------------------------------------------------------------- #
def _build_pool(prob, anchors, cache, per_pool, L, seed):
    """Refine each (precomputed) anchor into a Z1=0 elite; dedup near-identical -> a diverse
    population. `per_pool[k]` = per-anchor deadline (eval threshold or wall time)."""
    pop = []
    for k, (name, a) in enumerate(anchors):
        arng = random.Random(seed + 1009 * k)
        A, tot = _refine(prob, a, cache, per_pool[k], L, rng=arng)
        if not any(W.hamming(A, e[1]) < 1e-9 for e in pop):
            pop.append([name, A, tot])
    if not pop:
        ap = K.a_pref(prob)
        pop.append(["pref", ap, K.total_obj(prob, ap, cache)[0]])
    pop.sort(key=lambda e: e[2])
    return pop


def _evolve(prob, pop, cache, gen_dl, L, maxpop, rng):
    """Generation loop: recombine(best-ish x max-Hamming partner) -> polish -> RTR insert.
    Returns (best_assign, best_tot, gens)."""
    best, best_tot = dict(pop[0][1]), pop[0][2]
    op = os.environ.get("WEAVE_XOVER", "pr")
    mix_p = float(os.environ.get("WEAVE_MIX_P", "0.3"))
    gens = 0
    while K._within(gen_dl):
        p1 = _select(pop, rng)
        p2 = W.most_diverse(p1[1], pop)
        if p2[1] is p1[1] and len(pop) > 1:
            p2 = pop[-1]
        if op == "uniform" or (op == "mix" and rng.random() < mix_p):
            child = W.uniform_crossover(p1[1], p2[1], rng)
        else:
            lo, hi = (p1, p2) if p1[2] <= p2[2] else (p2, p1)
            ca, ta = W.path_relink(prob, lo[1], hi[1], cache, order="fixed")
            cb, tb = W.path_relink(prob, hi[1], lo[1], cache, order="fixed")
            child = ca if ta <= tb else cb
        c, ct = _polish(prob, child, cache, gen_dl, L, rng=rng)
        _insert(pop, c, ct, maxpop)
        if ct < best_tot - 1e-9:
            best, best_tot = dict(c), ct
        gens += 1
    return best, best_tot, gens


def refine_population(prob, anchors, timelimit, L=1, eval_limit=None, seed=0):
    """Portfolio WORKER entry: run a full WEAVE population from PRECOMPUTED anchors (no MIP
    solve here -> no Gurobi contention), NORECOMB (the master does one union-recombine).
    Returns (best_assign, pool_dict, best_tot). `seed` diversifies the island."""
    K._POOL.clear()
    K.clear_packing_caches()
    K._EVALS = 0
    K._EVAL_LIMIT = eval_limit
    cache = {}
    maxpop = int(os.environ.get("WEAVE_MAXPOP", "8"))
    pool_frac = float(os.environ.get("WEAVE_POOL_FRAC", "0.45"))
    n = max(1, len(anchors))
    if eval_limit is not None:
        per_pool = [eval_limit * pool_frac * (k + 1) / n for k in range(n)]
        gen_dl = eval_limit
    else:
        dl = time.time() + max(0.5, timelimit)
        pb = time.time() + (timelimit * pool_frac)
        per_pool = [time.time() + (timelimit * pool_frac) * (k + 1) / n for k in range(n)]
        gen_dl = dl
    rng = random.Random(seed)
    pop = _build_pool(prob, anchors, cache, per_pool, L, seed)
    best, best_tot, _ = _evolve(prob, pop, cache, gen_dl, L, maxpop, rng)
    return best, dict(K._POOL), best_tot


# --------------------------------------------------------------------------- #
# top-level solve
# --------------------------------------------------------------------------- #
def weave_solve(prob, timelimit, _return_assignment=False):
    K._EVALS = 0
    K._POOL.clear()
    K.clear_packing_caches()
    t0 = time.time()
    cache = {}

    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    K._EVAL_LIMIT = int(max_evals) if max_evals else None
    L = int(os.environ.get("WEAVE_LAHC_L", os.environ.get("SOLVER_LAHC_L", "1")))
    recomb_on = _HAS_GUROBI and not K._env_flag("SOLVER_NORECOMB")
    maxpop = int(os.environ.get("WEAVE_MAXPOP", "8"))
    seed = int(os.environ.get("SOLVER_SEED", "20260702"))
    rng = random.Random(seed)

    mip_tl = float(os.environ.get("WEAVE_MIP_TL", "4.0"))
    anchors = _anchors(prob, mip_tl, want_mip=not _flag("WEAVE_NO_MIP"))
    n_anchors = max(1, len(anchors))

    # budgeting (mirror PRISM): reserve time for final poly build + recombine; split the rest
    if K._EVAL_LIMIT is not None:
        pool_frac = float(os.environ.get("WEAVE_POOL_FRAC", "0.45"))
        pool_budget_total = K._EVAL_LIMIT * pool_frac
        per_pool = [pool_budget_total * (k + 1) / n_anchors for k in range(n_anchors)]
        gen_dl = K._EVAL_LIMIT
        poly_deadline = recomb_deadline = None
    else:
        safety = max(2.0, timelimit * 0.06)
        min_o1 = float("inf")
        for _, a in anchors:
            _, pb = K.total_obj(prob, a, cache)
            min_o1 = min(min_o1, sum(pb.values()))
        poly_floor = float(os.environ.get("SOLVER_POLY_FLOOR", "6.0"))
        poly_frac = float(os.environ.get("SOLVER_POLY_RESERVE", "0.30"))
        poly_reserve = (max(poly_floor, timelimit * poly_frac) if min_o1 > 1e-9
                        else max(1.0, timelimit * 0.04))
        recomb_cap = float(os.environ.get("SOLVER_RECOMB_CAP", "10.0"))
        recomb_floor = float(os.environ.get("SOLVER_RECOMB_FLOOR", "5.0"))
        recomb_reserve = (min(recomb_cap, max(recomb_floor, timelimit * 0.18)) if recomb_on else 0.0)
        if _return_assignment:
            poly_reserve = 0.0
        search_total = max(0.5, (timelimit - safety) - (time.time() - t0) - poly_reserve - recomb_reserve)
        pool_frac = float(os.environ.get("WEAVE_POOL_FRAC", "0.45"))
        pool_end = time.time() + search_total * pool_frac
        per_pool = [time.time() + (search_total * pool_frac) * (k + 1) / n_anchors for k in range(n_anchors)]
        gen_dl = time.time() + search_total
        recomb_deadline = t0 + timelimit - safety - poly_reserve
        poly_deadline = t0 + timelimit - safety

    # --- build diverse pool + evolve (shared helpers; see _build_pool / _evolve) -------
    pop = _build_pool(prob, anchors, cache, per_pool, L, seed)
    base_incumbent = dict(pop[0][1])            # best initial elite = trusted guard floor
    best, best_tot, gens = _evolve(prob, pop, cache, gen_dl, L, maxpop, rng)

    LAST_STATS.update({"n_anchors": n_anchors, "gens": gens, "pop": len(pop),
                       "best_tot": round(best_tot)})

    # --- guarded column-recombination over every (bay,set) piece the population generated
    if recomb_on:
        rdl = recomb_deadline if recomb_deadline is not None else (time.time() + float(
            os.environ.get("SOLVER_RECOMB_SOLVE_S", "8")))
        try:
            best = K._recombine(prob, best, deadline=rdl)
        except Exception:
            pass

    if _return_assignment:
        return best, base_incumbent

    # --- final true-objective best-of guard (Pareto-safe) + emit ----------------------
    cands = [best]
    if base_incumbent != best:
        cands.append(base_incumbent)
    try:
        win_obj, win_packed = K._score_and_pack(prob, cands[0], poly_deadline=poly_deadline)
        for c in cands[1:]:
            o, pk = K._score_and_pack(prob, c, poly_deadline=poly_deadline)
            if o < win_obj - 1e-9:
                win_obj, win_packed = o, pk
        return K._solution_from_packed(win_packed)
    except Exception:
        return K.build_solution(prob, best, poly_deadline=poly_deadline)
