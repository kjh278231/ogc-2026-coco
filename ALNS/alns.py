"""Kick-centric ALNS engine for the OGC 2026 assignment problem (prototype).

Rationale (see discussion): the production engine's "ILS" runs a full hill-climb
to convergence after every perturbation, so a 60s budget yields only ~2-3 kicks on
hard instances -- destroy-repair is NOT the central loop. This engine makes the
destroy->repair->accept cycle the spine: each iteration is ONE candidate evaluation
(a 1-pass repair, no convergence climb), so the same budget yields thousands of
iterations. Feasibility is guaranteed by the packer (footprint-disjoint), so any
assignment the repair produces is feasible-able -- the metaheuristic only juggles the
objective.

Reuses solver.py / packing.py wholesale (seeds, total_obj, the (bay,set) cache,
solve_bay, build_solution, the eval-count / wall machinery). Only the SEARCH loop is
new, so an A/B vs solver.framework_solve isolates "deep-few-kicks" vs "shallow-many-kicks".

Operators (this prototype; small set on purpose -- m is small and evals are expensive):
  destroy: worst-tardiness-bay (Z1, feedback-driven via perbay), worst-preference
           (Z3, instant), random (diversify).
  repair : preference-softmax 1-pass insertion (instant; stochastic so it does not
           just snap removed blocks back into the hot bay -> real exploration).
  accept : record-to-record travel (scale-robust; band decays to 0 over the budget).

Operator SELECTION is fixed-probability here (validate operators first); adaptive
roulette weights are the deliberate next step once operators are shown to help.
"""
from __future__ import annotations
import math
import os
import random
import time

import solver
from packing import (
    _MASK_R_SEARCH, _MASK_SEARCH, clear_packing_caches, extract_tardiness, fits,
    solve_bay,
)

try:
    from ortools.sat.python import cp_model as _cp_model
    _HAS_ORTOOLS = True
except Exception:                       # pragma: no cover
    _HAS_ORTOOLS = False

# Last-run diagnostics (read by the A/B runner): iteration/accept/operator counts.
LAST_STATS: dict = {}


def _options(blocks, bays, i):
    """Fitting bays for block i (fall back to all bays, mirroring a_pref)."""
    m = len(bays)
    opts = [j for j in range(m) if fits(blocks[i], bays[j])]
    return opts or list(range(m))


# --------------------------------------------------------------------------- #
# destroy operators -> return a list of removed block ids.
# Uniform signature (prob, cur, perbay, rng, k) so the weighted selector can call
# any of them interchangeably (perbay unused by some).
# --------------------------------------------------------------------------- #
_SHAW_SPANS: dict = {}   # id(prob) -> (span_release, span_due, span_workload, span_pref)


def _shaw_spans(prob):
    key = id(prob)
    v = _SHAW_SPANS.get(key)
    if v is not None:
        return v
    blocks = prob["blocks"]
    m = len(prob["bays"])
    rels = [b["release_time"] for b in blocks]
    dues = [b["due_date"] for b in blocks]
    wls = [b["workload"] for b in blocks]
    max_pref = max((max(b["bay_preferences"]) for b in blocks), default=1)
    v = (max(1.0, max(rels) - min(rels)), max(1.0, max(dues) - min(dues)),
         max(1.0, max(wls) - min(wls)), max(1.0, m * max_pref))
    _SHAW_SPANS[key] = v
    return v


_SHAW_NEIGHBORS: dict = {}   # id(prob) -> list[list[int]]: per seed, blocks sorted most-related-first


def _shaw_neighbors(prob):
    """Precomputed per-seed relatedness ranking. Relatedness depends only on STATIC block
    attributes (release/due/preference/workload), NOT on the current solution, so it is
    computed ONCE per solve (O(n^2 * m) + sorts, n<=~300 -> trivial). Each destroy then
    samples from a presorted list in O(k), instead of recomputing O(n*m) relatedness +
    O(n log n) sort every call -- which was eating Shaw's iteration budget."""
    key = id(prob)
    v = _SHAW_NEIGHBORS.get(key)
    if v is not None:
        return v
    blocks = prob["blocks"]
    n = len(blocks)
    m = len(prob["bays"])
    sr, sd, sw, sp = _shaw_spans(prob)
    wt = float(os.environ.get("ALNS_SHAW_T", "3.0"))
    wp = float(os.environ.get("ALNS_SHAW_P", "5.0"))
    wlw = float(os.environ.get("ALNS_SHAW_L", "2.0"))
    rel = [b["release_time"] for b in blocks]
    due = [b["due_date"] for b in blocks]
    wl = [b["workload"] for b in blocks]
    prefs = [b["bay_preferences"] for b in blocks]
    twden = sr + sd
    neigh = [None] * n
    for s in range(n):
        ps, rs, ds, ws = prefs[s], rel[s], due[s], wl[s]
        scored = []
        for i in range(n):
            if i == s:
                continue
            tw = abs(rs - rel[i]) + abs(ds - due[i])
            pi = prefs[i]
            pd = 0
            for j in range(m):
                pd += abs(ps[j] - pi[j])
            R = wt * tw / twden + wp * pd / sp + wlw * abs(ws - wl[i]) / sw
            scored.append((R, i))
        scored.sort()
        neigh[s] = [i for _, i in scored]
    _SHAW_NEIGHBORS[key] = neigh
    return neigh


def _destroy_worst_tardy(prob, cur, perbay, rng, k):
    """Remove k blocks from the most-tardy bay (Z1 is the dominant term and has no
    cheap surrogate, so target the bay the ACTUAL packing reports as late). Falls
    back to worst-preference when nothing is tardy (the Z1=0 instances)."""
    tardy = [j for j, t in perbay.items() if t > 0]
    if not tardy:
        return _destroy_worst_pref(prob, cur, perbay, rng, k)
    j = max(tardy, key=lambda j: perbay[j])
    ids = [i for i in cur if cur[i] == j]
    if len(ids) <= k:
        return ids
    return rng.sample(ids, k)


def _destroy_worst_pref(prob, cur, perbay, rng, k):
    """Remove blocks sitting far from their preferred bay (high Z3 contribution).
    Slightly randomized over the top band so it is not deterministic."""
    blocks = prob["blocks"]
    loss = {i: max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][cur[i]]
            for i in cur}
    ranked = sorted(cur, key=lambda i: -loss[i])
    band = ranked[:max(k, min(len(ranked), k * 3))]
    return rng.sample(band, min(k, len(band)))


def _destroy_random(prob, cur, perbay, rng, k):
    ids = list(cur)
    return rng.sample(ids, min(k, len(ids)))


def _destroy_shaw(prob, cur, perbay, rng, k):
    """Shaw / related removal via the PRECOMPUTED relatedness ranking: pick a seed, then
    sample k blocks from its presorted neighbor list with a rank^3 bias toward the most
    related -- O(k) per call. Relatedness blends time-window closeness (Z1 contention),
    preference-vector distance (Z3), and workload distance (Z2). Removing an interacting
    cluster lets the repair reshuffle them jointly. (cur is always a complete assignment,
    so the seed's neighbor list already covers every other block.)"""
    ids = list(cur)
    if len(ids) <= k:
        return ids
    neigh = _shaw_neighbors(prob)
    s = rng.choice(ids)
    # neigh[s] is precomputed == the old per-call `sorted(others, key=relatedness)` list
    # (relatedness is solution-independent; ties break on block index in both), so the old
    # pop-without-replacement loop on it is BIT-IDENTICAL to the original shaw -- only the
    # O(n*m) relatedness recompute + O(n log n) sort are skipped, not the behaviour.
    pool = list(neigh[s])
    chosen = [s]
    while len(chosen) < k and pool:
        idx = int((rng.random() ** 3) * len(pool))   # rank^3 -> bias to most-related front
        chosen.append(pool.pop(idx))
    return chosen


def _destroy_bay(prob, cur, perbay, rng, k):
    """Bay-emptying (route_removal analog): remove a concentrated chunk from a single
    bay (biased toward the worst-tardy / most-loaded one) so the repair can redistribute
    a whole sub-load -- a move the one-block relocation cannot make. Bounded so it stays
    bay-localized and the per-iteration re-pack cost stays affordable."""
    blocks = prob["blocks"]
    m = len(prob["bays"])
    tardy = [j for j, t in perbay.items() if t > 0]
    if tardy and rng.random() < 0.7:
        j = max(tardy, key=lambda j: perbay[j])
    else:
        loads = [0.0] * m
        for i, b in cur.items():
            loads[b] += blocks[i]["workload"]
        j = max(range(m), key=lambda j: loads[j]) if rng.random() < 0.5 else rng.randrange(m)
    ids = [i for i in cur if cur[i] == j]
    if not ids:
        return _destroy_random(prob, cur, perbay, rng, k)
    nrm = min(len(ids), rng.randint(k, max(k, 3 * k)))
    return rng.sample(ids, nrm)


# --------------------------------------------------------------------------- #
# repair: 1-pass preference-softmax insertion (instant -- no packing here; the
# single total_obj() call after repair does the one packing evaluation)
# --------------------------------------------------------------------------- #
def _repair_softmax(prob, cur, removed, rng, tau):
    blocks = prob["blocks"]
    bays = prob["bays"]
    cand = dict(cur)
    for i in removed:
        cand.pop(i, None)
    # insert hardest-to-place first (fewest options, then heaviest) -- regret-ish.
    order = sorted(removed, key=lambda i: (len(_options(blocks, bays, i)),
                                           -blocks[i]["workload"]))
    for i in order:
        opts = _options(blocks, bays, i)
        prefs = blocks[i]["bay_preferences"]
        mx = max(prefs[j] for j in opts)
        weights = [math.exp((prefs[j] - mx) / tau) for j in opts]  # in (0,1]; safe
        tot = sum(weights)
        r = rng.random() * tot
        acc = 0.0
        chosen = opts[-1]
        for j, w in zip(opts, weights):
            acc += w
            if r <= acc:
                chosen = j
                break
        cand[i] = chosen
    return cand


def _bay_T(prob, j, ids_tuple, pack_cache):
    """Tardiness of bay j packed with exactly `ids_tuple` (sorted), memoized by
    (bay, set) -- packing depends on the bay geometry, so the key includes j."""
    key = (j, ids_tuple)
    v = pack_cache.get(key)
    if v is not None:
        return v
    if not ids_tuple:
        pack_cache[key] = 0.0
        return 0.0
    placed = solve_bay(prob, j, list(ids_tuple), mask=_MASK_SEARCH, mask_R=_MASK_R_SEARCH)
    T, _ = extract_tardiness(prob, j, placed)
    pack_cache[key] = T
    solver._POOL[(j, ids_tuple)] = T   # feed the set-partitioning recombine column pool
    return T


def _repair_z1greedy(prob, cur, removed, n_promising, pack_cache):
    """Z1-aware insertion: for each removed block, among its top-`n_promising`
    preferred fitting bays, ACTUALLY re-pack each candidate bay (current set + block)
    and insert where the TRUE incremental cost w1*ΔT + w3*pref_loss is least. This is
    the routing repair's cheapest-feasible-insertion philosophy adapted to our setting
    where the insertion cost (tardiness) has no cheap surrogate (EXP 2) and must be
    measured by packing. Returns (cand, obj, perbay) computed from the same packs (no
    extra eval). Costs more packs/iteration than the softmax proxy, but defends Z1."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    w = prob["weights"]
    cand = dict(cur)
    for i in removed:
        cand.pop(i, None)
    bay_sets = {j: sorted(i for i in cand if cand[i] == j) for j in range(m)}
    mxpref = [max(b["bay_preferences"]) for b in blocks]
    # hardest-to-place first (fewest options, then heaviest)
    order = sorted(removed, key=lambda i: (len(_options(blocks, bays, i)),
                                           -blocks[i]["workload"]))
    for i in order:
        opts = _options(blocks, bays, i)
        promising = sorted(opts, key=lambda j: -blocks[i]["bay_preferences"][j])[:max(1, n_promising)]
        best_j, best_c = promising[0], float("inf")
        for j in promising:
            cur_set = bay_sets[j]
            t_old = _bay_T(prob, j, tuple(cur_set), pack_cache)
            t_new = _bay_T(prob, j, tuple(sorted(cur_set + [i])), pack_cache)
            c = w["w1"] * (t_new - t_old) + w["w3"] * (mxpref[i] - blocks[i]["bay_preferences"][j])
            if c < best_c - 1e-9:
                best_c, best_j = c, j
        cand[i] = best_j
        bay_sets[best_j] = sorted(bay_sets[best_j] + [i])
    perbay = {j: _bay_T(prob, j, tuple(bay_sets[j]), pack_cache) for j in range(m)}
    o2, o3 = solver.obj23(prob, cand)
    obj = w["w1"] * sum(perbay.values()) + w["w2"] * o2 + w["w3"] * o3
    return cand, obj, perbay


# --------------------------------------------------------------------------- #
# CP-SAT (OR-Tools) set-partitioning recombination -- the cyclic "MIP master".
# --------------------------------------------------------------------------- #
def _recombine_ortools(prob, best, deadline):
    """Z2-aware set-partitioning recombination over solver._POOL pieces, via OR-Tools
    CP-SAT (no license/size limit -> works locally where the demo Gurobi license rejects
    big models). Recovered from the project's pre-Gurobi recombine (commit 58f7c9e^) and
    kept guarded by solver._bestof_obj (adopt only if the TRUE objective improves ->
    Pareto-safe) with the adopted Z3-aligned cost prune (solver._pool_prune_key)."""
    if not _HAS_ORTOOLS:
        return best
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    n = len(blocks)
    w = prob["weights"]
    SCALE = 100
    cols = [(j, ids, T) for (j, ids), T in solver._POOL.items() if ids]
    if not cols:
        return best
    per_bay = int(os.environ.get("SOLVER_POOL_PER_BAY", "1000"))
    if per_bay > 0 and len(cols) > per_bay * m:
        inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
               for j in range(m) if any(best[i] == j for i in best)}
        key = solver._pool_prune_key(prob, T_index=2, ids_index=1)
        bins = [[] for _ in range(m)]
        for c in cols:
            bins[c[0]].append(c)
        cols = []
        for j in range(m):
            b = bins[j]
            b.sort(key=key)
            keep = b[:per_bay]
            kept = {(c[0], c[1]) for c in keep}
            keep.extend(c for c in b if (c[0], c[1]) in inc and (c[0], c[1]) not in kept)
            cols.extend(keep)
    model = _cp_model.CpModel()
    x = [model.NewBoolVar(f"c{k}") for k in range(len(cols))]
    by_block = [[] for _ in range(n)]
    by_bay = [[] for _ in range(m)]
    wl = []
    cost = []
    for k, (j, ids, T) in enumerate(cols):
        for i in ids:
            by_block[i].append(k)
        by_bay[j].append(k)
        wl.append(int(round(sum(blocks[i]["workload"] for i in ids))))
        pl = sum(max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j] for i in ids)
        cost.append(int(SCALE * (w["w1"] * T + w["w3"] * pl)))
    for i in range(n):
        if not by_block[i]:
            return best
        model.Add(sum(x[k] for k in by_block[i]) == 1)
    for j in range(m):
        if by_bay[j]:
            model.Add(sum(x[k] for k in by_bay[j]) <= 1)
    obj = sum(x[k] * cost[k] for k in range(len(cols)))
    if m >= 2:
        areas = [b["width"] * b["height"] for b in bays]
        avg = sum(areas) / m
        cj = [round(avg / areas[j] * SCALE) for j in range(m)]
        sload = [sum(cj[j] * wl[k] * x[k] for k in by_bay[j]) for j in range(m)]
        M = model.NewIntVar(0, 10 ** 12, "M")     # = SCALE * Z2
        for a in range(m):
            for b in range(m):
                if a != b:
                    model.Add(M >= sload[a] - sload[b])
        obj = obj + int(w["w2"]) * M
    model.Minimize(obj)
    inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
           for j in range(m) if any(best[i] == j for i in best)}
    for k, (j, ids, T) in enumerate(cols):
        model.AddHint(x[k], 1 if (j, ids) in inc else 0)
    cp = _cp_model.CpSolver()
    cp.parameters.num_search_workers = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
    cp.parameters.random_seed = int(os.environ.get("SOLVER_SEED", "0"))
    if deadline is None:
        # deterministic solve (validation): a DETERMINISTIC-time budget, not wall-clock, so
        # the recombine is reproducible run-to-run. Pair with SOLVER_CP_WORKERS=1 (parallel
        # CP-SAT is non-deterministic even with a fixed seed).
        cp.parameters.max_deterministic_time = float(os.environ.get("SOLVER_RECOMB_DET_T", "120"))
    else:
        cap = float(os.environ.get("SOLVER_RECOMB_SOLVE_S", "8"))
        cp.parameters.max_time_in_seconds = min(cap, max(0.5, deadline - time.time()))
    st = cp.Solve(model)
    if st not in (_cp_model.OPTIMAL, _cp_model.FEASIBLE):
        return best
    A = {}
    for k, (j, ids, T) in enumerate(cols):
        if cp.Value(x[k]) == 1:
            for i in ids:
                A[i] = j
    if len(A) != n:
        return best
    if solver._bestof_obj(prob, A, deadline) < solver._bestof_obj(prob, best, deadline) - 1e-9:
        return A
    return best


def _recombine_gurobi(prob, best, deadline):
    """Same Z2-aware set-partitioning recombination as _recombine_ortools, but solved with
    Gurobi (reuses solver's lazy WLS env). Determinism for validation: Threads=1 + fixed
    Seed + a deterministic WorkLimit when deadline is None (Gurobi's TimeLimit is wall-clock
    and non-reproducible). Guarded by solver._bestof_obj; same Z3-aligned cost prune."""
    if not solver._HAS_GUROBI:
        return best
    gp, GRB = solver._gp, solver._GRB
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    n = len(blocks)
    w = prob["weights"]
    SCALE = 100
    cols = [(j, ids, T) for (j, ids), T in solver._POOL.items() if ids]
    if not cols:
        return best
    per_bay = int(os.environ.get("SOLVER_POOL_PER_BAY", "1000"))
    if per_bay > 0 and len(cols) > per_bay * m:
        inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
               for j in range(m) if any(best[i] == j for i in best)}
        key = solver._pool_prune_key(prob, T_index=2, ids_index=1)
        bins = [[] for _ in range(m)]
        for c in cols:
            bins[c[0]].append(c)
        cols = []
        for j in range(m):
            b = bins[j]
            b.sort(key=key)
            keep = b[:per_bay]
            kept = {(c[0], c[1]) for c in keep}
            keep.extend(c for c in b if (c[0], c[1]) in inc and (c[0], c[1]) not in kept)
            cols.extend(keep)
    model = gp.Model(env=solver._grb_env())
    x = [model.addVar(vtype=GRB.BINARY, name=f"c{k}") for k in range(len(cols))]
    by_block = [[] for _ in range(n)]
    by_bay = [[] for _ in range(m)]
    wl = []
    cost = []
    for k, (j, ids, T) in enumerate(cols):
        for i in ids:
            by_block[i].append(k)
        by_bay[j].append(k)
        wl.append(sum(blocks[i]["workload"] for i in ids))
        pl = sum(max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j] for i in ids)
        cost.append(int(SCALE * (w["w1"] * T + w["w3"] * pl)))
    for i in range(n):
        if not by_block[i]:
            model.dispose()
            return best
        model.addConstr(gp.quicksum(x[k] for k in by_block[i]) == 1)
    for j in range(m):
        if by_bay[j]:
            model.addConstr(gp.quicksum(x[k] for k in by_bay[j]) <= 1)
    obj = gp.quicksum(cost[k] * x[k] for k in range(len(cols)))
    if m >= 2:
        areas = [b["width"] * b["height"] for b in bays]
        avg = sum(areas) / m
        cj = [round(avg / areas[j] * SCALE) for j in range(m)]
        sload = [gp.quicksum(cj[j] * wl[k] * x[k] for k in by_bay[j]) for j in range(m)]
        M = model.addVar(lb=0, ub=10 ** 12, vtype=GRB.INTEGER, name="M")
        for a in range(m):
            for b in range(m):
                if a != b:
                    model.addConstr(M >= sload[a] - sload[b])
        obj = obj + w["w2"] * M
    model.setObjective(obj, GRB.MINIMIZE)
    inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
           for j in range(m) if any(best[i] == j for i in best)}
    for k, (j, ids, T) in enumerate(cols):
        x[k].Start = 1 if (j, ids) in inc else 0
    model.Params.Threads = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
    model.Params.Seed = int(os.environ.get("SOLVER_SEED", "0"))
    if deadline is None:
        # deterministic work-unit budget (reproducible), not wall-clock TimeLimit
        model.Params.WorkLimit = float(os.environ.get("SOLVER_RECOMB_DET_W", "60"))
    else:
        cap = float(os.environ.get("SOLVER_RECOMB_SOLVE_S", "8"))
        model.Params.TimeLimit = min(cap, max(0.5, deadline - time.time()))
    model.optimize()
    if model.SolCount == 0:
        model.dispose()
        return best
    A = {}
    for k, (j, ids, T) in enumerate(cols):
        if x[k].X > 0.5:
            for i in ids:
                A[i] = j
    model.dispose()
    if len(A) != n:
        return best
    if solver._bestof_obj(prob, A, deadline) < solver._bestof_obj(prob, best, deadline) - 1e-9:
        return A
    return best


def _recombine(prob, best, deadline):
    """Dispatch to the configured recombine backend (ALNS_RECOMB_BACKEND, default gurobi),
    falling back to whichever solver is available."""
    backend = os.environ.get("ALNS_RECOMB_BACKEND", "gurobi")
    if backend == "gurobi" and solver._HAS_GUROBI:
        return _recombine_gurobi(prob, best, deadline)
    if _HAS_ORTOOLS:
        return _recombine_ortools(prob, best, deadline)
    if solver._HAS_GUROBI:
        return _recombine_gurobi(prob, best, deadline)
    return best


def _mip_assign(prob, deadline):
    """The Gurobi assignment MIP ONLY (min w2*Z2 + w3*Z3), no Z1 repair -- the cheap probe
    (~2s) used to gate whether the expensive repair is worth running. Returns the (low-Z3,
    high-Z1) assignment A or None. Deterministic: Threads=SOLVER_CP_WORKERS(=1) + Seed +
    WorkLimit when deadline is None."""
    if not solver._HAS_GUROBI:
        return None
    gp, GRB = solver._gp, solver._GRB
    blocks = prob["blocks"]
    bays = prob["bays"]
    w = prob["weights"]
    n = len(blocks)
    m = len(bays)
    try:
        areas = [b["width"] * b["height"] for b in bays]
        avg = sum(areas) / m
        u = [avg / areas[j] for j in range(m)]
        md = gp.Model(env=solver._grb_env())
        x = [[md.addVar(vtype=GRB.BINARY) for j in range(m)] for i in range(n)]
        for i in range(n):
            md.addConstr(gp.quicksum(x[i][j] for j in range(m)) == 1)
            for j in range(m):
                if not fits(blocks[i], bays[j]):
                    md.addConstr(x[i][j] == 0)
        load = [gp.quicksum(blocks[i]["workload"] * x[i][j] for i in range(n)) for j in range(m)]
        Mv = md.addVar(lb=0)
        for a in range(m):
            for b in range(m):
                if a != b:
                    md.addConstr(Mv >= u[a] * load[a] - u[b] * load[b])
        pref = gp.quicksum(
            (max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]) * x[i][j]
            for i in range(n) for j in range(m))
        md.setObjective(w["w2"] * Mv + w["w3"] * pref, GRB.MINIMIZE)
        md.Params.Threads = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
        md.Params.Seed = int(os.environ.get("SOLVER_SEED", "0"))
        if deadline is None:
            md.Params.WorkLimit = float(os.environ.get("SOLVER_MIP_DET_W", "30"))
        else:
            md.Params.TimeLimit = max(0.5, deadline - time.time())
        md.optimize()
        if md.SolCount == 0:
            md.dispose()
            return None
        A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
        md.dispose()
        return A
    except Exception:
        return None


def _mip_repair_z1(prob, A, deadline, cache=None):
    """Repair the Z1 of a (low-Z3) MIP assignment via FULL-sweep-convergence local_search
    (patience=None -- a small patience stopped far too early, left z1=303 on prob_20).
    Deterministic as long as the generous deadline does not bind (convergence happens first)."""
    try:
        rc = cache if cache is not None else {}
        rdl = (time.time() + 120.0) if deadline is None else deadline
        A2, _ = solver.local_search(prob, A, rc, rdl)
        return A2
    except Exception:
        return A


def _mip_repair(prob, deadline, cache=None):
    """MIP assignment + full Z1 repair (used by the operator)."""
    A = _mip_assign(prob, deadline)
    if A is None:
        return None
    return _mip_repair_z1(prob, A, deadline, cache)


# --------------------------------------------------------------------------- #
# main solve
# --------------------------------------------------------------------------- #
def alns_solve(prob, timelimit, _return_assignment=False):
    """Time/eval-managed ALNS. Mirrors framework_solve's budget machinery so a fixed
    SOLVER_MAX_EVALS A/B is deterministic and comparable. Always returns a feasible
    solution dict (or the best assignment in _return_assignment worker mode)."""
    solver._EVALS = 0
    solver._POOL.clear()
    _SHAW_SPANS.clear()
    _SHAW_NEIGHBORS.clear()
    clear_packing_caches()
    t0 = time.time()
    cache: dict = {}

    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    solver._EVAL_LIMIT = int(max_evals) if max_evals else None

    # best heuristic seed as a feasible floor / start (min full objective).
    best, best_tot = None, float("inf")
    for fn in (solver.a_pref, solver.a_balanced_load, solver.a_pref_capped):
        a = fn(prob)
        tot, _ = solver.total_obj(prob, a, cache)
        if tot < best_tot:
            best, best_tot = a, tot

    # MIP-repair SEED (ALNS_MIP_SEED): add the production MIP-repair basin (global low-Z3,
    # Z1-repaired) as an extra starting incumbent -> ALNS refines around it AND feeds the
    # recombine pool low-Z3 columns. Targets prob_20 (where the column-pool recombine misses
    # the global low-Z3 basin the MIP reaches). One MIP solve at start.
    # MIP-repair seed with a CHEAP GATE: solve the MIP probe (~2-4s) first, then spend the
    # expensive Z1 repair ONLY if the MIP's best-case total (w2*Z2 + w3*Z3, Z1 assumed
    # repairable to ~0) beats the heuristic seed best. a_pref already nails low-Z1/Z3~0
    # instances (MIP can't beat it -> skip, no wasted budget); it is Z1-overloaded exactly
    # where the MIP basin wins (prob_20/13) -> spend. stat_mip_seed: 1=spent, -1=gated out.
    stat_mip_seed = 0
    if os.environ.get("ALNS_MIP_SEED", "0") not in ("0", "", "false", "off", "no"):
        _det = int(os.environ.get("ALNS_MAX_ITERS", "0")) > 0 or os.environ.get("SOLVER_MAX_EVALS")
        _wgt = prob["weights"]
        if _det:
            solve_dl = rep_dl = None
        else:
            solve_dl = t0 + float(os.environ.get("ALNS_MIP_SOLVE_BUDGET", "4"))
            _rb = os.environ.get("ALNS_MIP_SEED_BUDGET")
            # budget is a CAP, not a fixed spend: local_search stops at convergence, so the
            # actual cost = min(convergence_time, budget). The Z1 repair needs ~12-15s to
            # converge (prob_20), so T*0.15 (=9s @ T=60) cuts it off; T*0.25 (=15s) converges.
            # Cap at 24s: repair-convergence is instance-size-bound (not timelimit-bound), so
            # ~24s covers full convergence on any instance -- more is wasted.
            rep_dl = t0 + (float(_rb) if _rb else min(24.0, timelimit * 0.25))
        A_mip = _mip_assign(prob, solve_dl)
        if A_mip is not None:
            o2, o3 = solver.obj23(prob, A_mip)
            est = _wgt["w2"] * o2 + _wgt["w3"] * o3   # MIP best-case total (Z1 -> ~0)
            gate = float(os.environ.get("ALNS_MIP_SEED_GATE", "1.0"))
            if est < best_tot * gate:
                a = _mip_repair_z1(prob, A_mip, rep_dl, cache={})
                tot, _ = solver.total_obj(prob, a, cache)
                stat_mip_seed = 1
                if tot < best_tot:
                    best, best_tot = a, tot
            else:
                stat_mip_seed = -1

    recomb_mode = os.environ.get("ALNS_RECOMB", "off")       # off | final | cyclic
    recomb_cap = float(os.environ.get("ALNS_RECOMB_CAP", "6.0"))
    recomb_on = recomb_mode in ("final", "cyclic") and (solver._HAS_GUROBI or _HAS_ORTOOLS)
    # ALNS_MAX_ITERS>0 = DETERMINISTIC VALIDATION mode: stop after exactly N loop
    # iterations (no wall-clock dependence) AND still run a deterministic final recombine
    # (pair with SOLVER_CP_WORKERS=1). This is the canonical reproducible A/B -- unlike the
    # legacy SOLVER_MAX_EVALS mode it does NOT skip recombine (the dominant lever).
    det_iters = int(os.environ.get("ALNS_MAX_ITERS", "0"))

    # budget split (3 modes): det-iters validation / legacy eval (recombine skipped) / wall.
    if det_iters > 0:
        search_dl = None
        poly_deadline = None
        recomb_hardstop = None
    elif solver._EVAL_LIMIT is not None:
        recomb_on = False   # legacy eval mode keeps the old search-only determinism
        search_dl = solver._EVAL_LIMIT
        poly_deadline = None
        recomb_hardstop = None
    else:
        safety = max(1.5, timelimit * float(os.environ.get("ALNS_SAFETY_FRAC", "0.06")))
        poly_deadline = t0 + timelimit - safety
        tb = time.time()
        solver._score_and_pack(prob, best, poly_deadline=poly_deadline)
        build_cost = time.time() - tb
        # reserve factor x the measured build for the final pack. (_return_assignment
        # callers also reserve it: the portfolio worker self-packs+scores in this
        # window after alns_solve returns the assignment.)
        _bf = float(os.environ.get("ALNS_BUILD_MARGIN_FACTOR", "2.5"))
        build_margin = max(1.0, build_cost * _bf)
        # hard stop for any recombine solve so the final build margin is preserved;
        # reserve one recombine slot before the search deadline for the final master solve.
        recomb_hardstop = poly_deadline - build_margin
        recomb_reserve = (recomb_cap + 1.0) if recomb_on else 0.0
        search_dl = poly_deadline - build_margin - recomb_reserve

    rng = random.Random(int(os.environ.get("SOLVER_SEED", "0")))
    k_min = int(os.environ.get("ALNS_K_MIN", "2"))
    k_max = int(os.environ.get("ALNS_K_MAX", "6"))
    tau = float(os.environ.get("ALNS_REPAIR_TAU", "1.0"))
    band0 = float(os.environ.get("ALNS_RRT_BAND", "0.05"))
    # destroy operator weights (relative; normalized by the selector). Default 5-op set;
    # set ALNS_P_SHAW=0 ALNS_P_BAY=0 to recover the original 3-op set for an A/B.
    _ops_all = [
        ("tardy", float(os.environ.get("ALNS_P_TARDY", "0.30")), _destroy_worst_tardy),
        ("pref", float(os.environ.get("ALNS_P_PREF", "0.25")), _destroy_worst_pref),
        ("random", float(os.environ.get("ALNS_P_RANDOM", "0.15")), _destroy_random),
        ("shaw", float(os.environ.get("ALNS_P_SHAW", "0.20")), _destroy_shaw),
        ("bay", float(os.environ.get("ALNS_P_BAY", "0.0")), _destroy_bay),   # confirmed harmful -> off by default
    ]
    destroy_ops = [(nm, pr, fn) for nm, pr, fn in _ops_all if pr > 0]
    _tot_p = sum(pr for _, pr, _ in destroy_ops) or 1.0
    repair_kind = os.environ.get("ALNS_REPAIR", "softmax")   # softmax | softmax_ls | z1greedy
    n_promising = int(os.environ.get("ALNS_N_PROMISING", "3"))
    ls_patience = int(os.environ.get("ALNS_LS_PATIENCE", "30"))
    # When no bay is tardy, EXCLUDE the tardy operator from selection entirely instead of
    # letting it fall back to a pref move -- avoids duplicating pref (z1=0 instances) and
    # keeps the adaptive credit for 'tardy' clean (only scored when it truly targets Z1).
    tardy_exclude = os.environ.get("ALNS_TARDY_EXCLUDE", "1") not in ("0", "", "false", "off", "no")
    recomb_period = float(os.environ.get("ALNS_RECOMB_PERIOD", "12.0"))  # wall seconds
    recomb_stall = int(os.environ.get("ALNS_RECOMB_STALL", "400"))       # iters w/o new best
    mip_op = os.environ.get("ALNS_MIP_OP", "0") not in ("0", "", "false", "off", "no")
    mip_op_period = int(os.environ.get("ALNS_MIP_OP_PERIOD", "500"))     # iters between injections
    mip_op_cap = float(os.environ.get("ALNS_MIP_OP_CAP", "5.0"))         # wall cap per solve
    pack_cache: dict = {}

    # Adaptive operator selection (ALNS "A"): roulette by weights learned from recent
    # performance. weights start at the configured probs (sensible prior); when adaptive,
    # they are nudged every adapt_period iters toward each operator's average segment score
    # (new global best=s1, improved current=s2, accepted-worse=s3, rejected=0), floored at
    # w_min so no operator is permanently killed. ALNS_ADAPTIVE=0 -> weights never change
    # -> bit-identical to fixed-probability selection.
    adaptive = os.environ.get("ALNS_ADAPTIVE", "0") not in ("0", "", "false", "off", "no")
    adapt_period = int(os.environ.get("ALNS_ADAPT_PERIOD", "100"))
    adapt_mode = os.environ.get("ALNS_ADAPT_MODE", "fresh")   # fresh (success-rate + smoothing) | blend
    adapt_react = float(os.environ.get("ALNS_ADAPT_REACT", "0.2"))
    adapt_smooth = float(os.environ.get("ALNS_ADAPT_SMOOTH", "0.05"))   # per-op probability floor
    sig = (float(os.environ.get("ALNS_ADAPT_S1", "20")),
           float(os.environ.get("ALNS_ADAPT_S2", "10")),
           float(os.environ.get("ALNS_ADAPT_S3", "2")))
    w_min = float(os.environ.get("ALNS_ADAPT_WMIN", "0.05"))
    weights = {nm: pr for nm, pr, _ in destroy_ops}
    fn_by_name = {nm: fn for nm, _, fn in destroy_ops}
    seg_score = {nm: 0.0 for nm in weights}
    seg_use = {nm: 0 for nm in weights}

    cur = dict(best)
    cur_tot, perbay = solver.total_obj(prob, cur, cache)
    iters = 0
    accepts = 0
    op_calls = {nm: 0 for nm, _, _ in destroy_ops}
    op_best = {nm: 0 for nm, _, _ in destroy_ops}
    stall = 0
    last_recomb = time.time()
    recomb_attempts = 0
    recomb_wins = 0
    mip_op_attempts = 0
    mip_op_wins = 0

    n0 = solver._now()
    span = max(1e-9, (search_dl - n0)) if search_dl is not None else 1.0
    try:
        while (iters < det_iters) if det_iters > 0 else solver._within(search_dl):
            iters += 1
            k = rng.randint(k_min, k_max)
            if tardy_exclude and not any(t > 0 for t in perbay.values()):
                elig = [nm for nm, _, _ in destroy_ops if nm != "tardy"]
            else:
                elig = [nm for nm, _, _ in destroy_ops]
            if not elig:
                elig = [nm for nm, _, _ in destroy_ops]
            tw = 0.0
            for nm in elig:
                tw += weights[nm]
            r = rng.random() * tw
            acc = 0.0
            name = elig[-1]
            for nm in elig:
                acc += weights[nm]
                if r <= acc:
                    name = nm
                    break
            removed = fn_by_name[name](prob, cur, perbay, rng, k)
            op_calls[name] += 1
            if not removed:
                continue
            if repair_kind == "z1greedy":
                cand, cand_tot, cand_perbay = _repair_z1greedy(
                    prob, cur, removed, n_promising, pack_cache)
            elif repair_kind == "softmax_ls":
                cand = _repair_softmax(prob, cur, removed, rng, tau)
                cand, _ = solver.local_search(prob, cand, cache, search_dl,
                                              patience=ls_patience)
                cand_tot, cand_perbay = solver.total_obj(prob, cand, cache)
            else:
                cand = _repair_softmax(prob, cur, removed, rng, tau)
                cand_tot, cand_perbay = solver.total_obj(prob, cand, cache)
            # record-to-record travel: accept within a (decaying) band of the best. Progress
            # is iteration-based in det mode (no wall dependence), else wall/eval-based.
            prog = (iters / det_iters) if det_iters > 0 else ((solver._now() - n0) / span)
            band = band0 * max(0.0, 1.0 - prog)
            prev_best, prev_cur = best_tot, cur_tot
            if cand_tot <= best_tot * (1.0 + band):
                cur, cur_tot, perbay = cand, cand_tot, cand_perbay
                accepts += 1
            if cand_tot < best_tot - 1e-9:
                best, best_tot = dict(cand), cand_tot
                op_best[name] += 1
                stall = 0
            else:
                stall += 1
            # adaptive scoring: reward this operator by outcome quality, update periodically.
            if adaptive:
                if cand_tot < prev_best - 1e-9:
                    sc = sig[0]
                elif cand_tot < prev_cur - 1e-9:
                    sc = sig[1]
                elif cand_tot <= prev_best * (1.0 + band):
                    sc = sig[2]
                else:
                    sc = 0.0
                seg_score[name] += sc
                seg_use[name] += 1
                if iters % adapt_period == 0:
                    if adapt_mode == "fresh":
                        # success-rate per call this segment, renormalized into a probability
                        # distribution with an additive (Laplace) smoothing floor so no
                        # operator's probability ever reaches 0:
                        #   p[i] = smooth + (1 - smooth*n) * rate[i]/sum(rate)
                        n_ops = len(weights)
                        rate = {nm: (seg_score[nm] / seg_use[nm]) if seg_use[nm] > 0 else 0.0
                                for nm in weights}
                        rsum = sum(rate.values())
                        for nm in weights:
                            if rsum <= 0:
                                weights[nm] = 1.0 / n_ops
                            else:
                                weights[nm] = adapt_smooth + (1.0 - adapt_smooth * n_ops) * (rate[nm] / rsum)
                    else:   # blend (reaction factor, with a floor)
                        for nm in weights:
                            if seg_use[nm] > 0:
                                weights[nm] = max(w_min, (1.0 - adapt_react) * weights[nm]
                                                  + adapt_react * (seg_score[nm] / seg_use[nm]))
                    for nm in weights:
                        seg_score[nm] = 0.0
                        seg_use[nm] = 0
            # cyclic set-partitioning master: periodically (or on stagnation) recombine
            # the accumulated column pool into a new global incumbent that the local
            # destroy-repair cannot reach. Guarded -> only adopts a true improvement.
            if (recomb_on and recomb_mode == "cyclic" and recomb_hardstop is not None
                    and time.time() + recomb_cap < recomb_hardstop
                    and (time.time() - last_recomb >= recomb_period or stall >= recomb_stall)):
                recomb_attempts += 1
                A = _recombine(prob, best, time.time() + recomb_cap)
                if A is not best:
                    a_tot, a_perbay = solver.total_obj(prob, A, cache)
                    if a_tot < best_tot - 1e-9:
                        best, best_tot = dict(A), a_tot
                        cur, cur_tot, perbay = dict(A), a_tot, a_perbay
                        recomb_wins += 1
                last_recomb = time.time()
                stall = 0
            # MIP-repair OPERATOR (ALNS_MIP_OP): periodically re-inject the global low-Z3
            # basin (guarded). Det mode -> deterministic WorkLimit; wall -> time-guarded.
            if (mip_op and iters % mip_op_period == 0
                    and (det_iters > 0 or (recomb_hardstop is not None
                                           and time.time() + mip_op_cap < recomb_hardstop))):
                mip_op_attempts += 1
                A = _mip_repair(prob, None if det_iters > 0 else time.time() + mip_op_cap, cache)
                if A is not None:
                    a_tot, a_perbay = solver.total_obj(prob, A, cache)
                    if a_tot < best_tot - 1e-9:
                        best, best_tot = dict(A), a_tot
                        cur, cur_tot, perbay = dict(A), a_tot, a_perbay
                        mip_op_wins += 1
    except Exception:
        pass  # keep the best feasible assignment found so far

    # final master recombine (both 'final' and 'cyclic' do one last full-pool solve). Wall
    # mode bounds it by the reserved slot; det-iters mode passes None -> deterministic solve.
    if recomb_on and (recomb_hardstop is not None or det_iters > 0):
        try:
            recomb_attempts += 1
            A = _recombine(prob, best, recomb_hardstop)
            if A is not best:
                a_tot, _ = solver.total_obj(prob, A, cache)
                if a_tot < best_tot - 1e-9:
                    best, best_tot = dict(A), a_tot
                    recomb_wins += 1
        except Exception:
            pass

    LAST_STATS.clear()
    LAST_STATS.update({"iters": iters, "accepts": accepts, "evals": solver._EVALS,
                       "op_calls": dict(op_calls), "op_best": dict(op_best),
                       "recomb_attempts": recomb_attempts, "recomb_wins": recomb_wins,
                       "mip_seed": stat_mip_seed, "mip_op_attempts": mip_op_attempts,
                       "mip_op_wins": mip_op_wins,
                       "weights": {nm: round(weights[nm], 3) for nm in weights},
                       "best_tot_proxy": best_tot})
    if _return_assignment:
        return best, dict(best)
    return solver.build_solution(prob, best, poly_deadline=poly_deadline)
