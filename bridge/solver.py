"""Self-contained OGC 2026 solver (depends only on utils.py).

Framework: assignment search (scored by a per-bay footprint-disjoint admission
packer) + best-of non-regression guard. Crane extraction is free by construction
because no two temporally-overlapping blocks share a footprint (AABB-disjoint),
which is affordable since bay area utilisation is low. See docs/technical_report.md.

Entry point: framework_solve(prob, timelimit) -> solution dict. Time-managed so it
never exceeds `timelimit`; always returns a feasible solution (disjoint placement
+ guaranteed empty-bay fallback).
"""
from __future__ import annotations
import time
import math
import os
import random
from packing import (
    _HAS_SHAPELY, _MASK_SEARCH, _MASK_R_SEARCH, _env_flag,
    clear_packing_caches, extract_tardiness, fits, solve_bay, solve_bay_best,
)

# Multi-order best-of packing (default OFF -> the search/build packer stays the single
# EDD order, bit-identical to the shipped path). When on, every per-bay pack (search proxy,
# recombine guard, and final build alike -- so there is no proxy seam) takes the best of a
# small set of principled orders, escalating only on contended bays. See packing.solve_bay_best.
_MULTIORDER = _env_flag("SOLVER_MULTIORDER")


try:
    import gurobipy as _gp
    from gurobipy import GRB as _GRB
    _HAS_GUROBI = True
except Exception:                       # pragma: no cover
    _HAS_GUROBI = False

# One silent Gurobi environment per process, started lazily so the WLS license is
# checked out exactly once and reused across every MIP solve (creating an env per
# model would re-checkout the license each call). OutputFlag=0 also suppresses the
# license banner and per-solve logs.
_GRB_ENV = None


def _grb_env():
    global _GRB_ENV
    if _GRB_ENV is None:
        _GRB_ENV = _gp.Env(empty=True)
        _GRB_ENV.setParam("OutputFlag", 0)
        _GRB_ENV.start()
    return _GRB_ENV


# Pool of (bay, block_set) -> AABB tardiness pieces seen during the search, for the
# Z2-aware set-partitioning recombination final step.
_POOL: dict = {}


# Search budgeting. By default the searches stop on a wall-clock DEADLINE (used for
# the real submission). For reproducible A/B EVALUATION, set SOLVER_MAX_EVALS: the
# searches then stop after a fixed number of candidate evaluations (total_obj calls)
# -> deterministic, no wall-clock variance. In that mode each `deadline` argument is
# reinterpreted as an eval-count threshold. Either way the harness should report the
# wall-clock time per problem (eval mode does NOT bound time -- it must be watched).
_EVALS = 0
_EVAL_LIMIT = None

# Anytime incumbent trace (analysis only, env-gated by SOLVER_TRACE; default off so the
# submission path is untouched). When on, total_obj appends (elapsed_s, objective) each
# time it sees a new global best, giving an x=time / y=objective curve for one run.
_TRACE: list = []
_TRACE_ON = False
_TRACE_T0 = 0.0
_TRACE_BEST = float("inf")


def _now():
    return _EVALS if _EVAL_LIMIT is not None else time.time()


def _within(x):
    """True while the search may continue. `x` is a time (default) or, in eval
    mode, an eval-count threshold."""
    return (_EVALS < x) if _EVAL_LIMIT is not None else (time.time() < x)


def _mid(deadline):
    """Halfway point between now and `deadline`, in the active unit (time/evals)."""
    n = _now()
    return n + (deadline - n) * 0.5




# --------------------------------------------------------------------------- #
# objective evaluation (Z2/Z3 instant; Z1 via per-bay packing, cached)
# --------------------------------------------------------------------------- #
def obj23(prob, assign):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    loads = [0.0] * m
    obj3 = 0.0
    for i, j in assign.items():
        loads[j] += blocks[i]["workload"]
        obj3 += max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    obj2 = math.floor(max((abs(u[a] * loads[a] - u[b] * loads[b])
                           for a in range(m) for b in range(m) if a != b), default=0.0))
    return obj2, obj3


def eval_obj1(prob, assign, cache):
    m = len(prob["bays"])
    obj1 = 0.0
    perbay = {}
    for j in range(m):
        ids = tuple(sorted(i for i, a in assign.items() if a == j))
        if not ids:
            perbay[j] = 0.0
            continue
        if ids in cache:
            perbay[j] = cache[ids]
        else:
            if _MULTIORDER:
                _, T, _ = solve_bay_best(prob, j, list(ids),
                                         mask=_MASK_SEARCH, mask_R=_MASK_R_SEARCH)
            else:
                T, _ = extract_tardiness(prob, j, solve_bay(
                    prob, j, list(ids), mask=_MASK_SEARCH, mask_R=_MASK_R_SEARCH))
            cache[ids] = T
            perbay[j] = T
        _POOL[(j, ids)] = perbay[j]   # record (bay, set) piece for recombination
        obj1 += perbay[j]
    return obj1, perbay


def total_obj(prob, assign, cache):
    global _EVALS, _TRACE_BEST
    _EVALS += 1
    obj1, perbay = eval_obj1(prob, assign, cache)
    obj2, obj3 = obj23(prob, assign)
    w = prob["weights"]
    tot = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    if _TRACE_ON and tot < _TRACE_BEST:
        _TRACE_BEST = tot
        _TRACE.append((time.time() - _TRACE_T0, tot))
    return tot, perbay


# --------------------------------------------------------------------------- #
# assignment heuristics
# --------------------------------------------------------------------------- #
def a_pref(prob):
    blocks = prob["blocks"]
    bays = prob["bays"]
    asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        asg[i] = next((j for j in order if fits(b, bays[j])), order[0])
    return asg


def a_balanced_load(prob):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    loads = [0.0] * m
    asg = {}
    for i in sorted(range(len(blocks)), key=lambda i: -blocks[i]["workload"]):
        b = blocks[i]
        cand = [j for j in range(m) if fits(b, bays[j])] or list(range(m))
        j = min(cand, key=lambda j: u[j] * (loads[j] + b["workload"]))
        asg[i] = j
        loads[j] += b["workload"]
    return asg


def a_pref_capped(prob, cap_factor=1.15):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    cap = math.ceil(len(blocks) / m * cap_factor)
    cnt = [0] * m
    asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        order = [j for j in order if fits(b, bays[j])] or order
        j = next((j for j in order if cnt[j] < cap), order[0])
        asg[i] = j
        cnt[j] += 1
    return asg


# --------------------------------------------------------------------------- #
# searches (deadline-driven; only accept improving moves)
# --------------------------------------------------------------------------- #
def local_search(prob, assign, cache, deadline, patience=None):
    """Focused hill climb: move blocks out of tardy bays (first improvement).
    With `patience` set (unified ILS loop), stop after that many CONSECUTIVE
    non-improving candidate evaluations -- a timing-independent convergence stop --
    keeping `deadline` only as a hard safety cap. patience=None preserves the legacy
    behaviour exactly (deadline-driven full-sweep convergence)."""
    m = len(prob["bays"])
    blocks = prob["blocks"]
    best = dict(assign)
    best_tot, perbay = total_obj(prob, best, cache)
    improved = True
    noimp = 0
    while improved and _within(deadline):
        improved = False
        tardy = [j for j in range(m) if perbay.get(j, 0) > 0]
        movers = [i for i in best if best[i] in tardy]
        for i in movers:
            if not _within(deadline):
                break
            for j in range(m):
                if j == best[i] or not fits(blocks[i], prob["bays"][j]):
                    continue
                trial = dict(best)
                trial[i] = j
                tot, _ = total_obj(prob, trial, cache)
                if tot < best_tot - 1e-9:
                    best, best_tot = trial, tot
                    _, perbay = total_obj(prob, best, cache)
                    improved = True
                    noimp = 0
                    break
                elif patience is not None:
                    noimp += 1
                    if noimp >= patience:
                        return best, best_tot
    return best, best_tot


def improved_search(prob, cache, deadline):
    """Two-phase hill climb from the best heuristic seed. Phase 1 (first half):
    movers in tardy bays (Z1) + the max-(u*load) bay (Z2). Phase 2: also move
    blocks off their preferred bay (Z3), tried preference-first."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur, cur_tot = None, float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob)
        tot, _ = total_obj(prob, a, cache)
        if tot < cur_tot:
            cur, cur_tot = dict(a), tot
    _, perbay = total_obj(prob, cur, cache)

    def hillclimb(include_offpref, sub_deadline):
        nonlocal cur, cur_tot, perbay
        improved = True
        while improved and _within(sub_deadline):
            improved = False
            loads = [0.0] * m
            for i, j in cur.items():
                loads[j] += blocks[i]["workload"]
            maxload = max(range(m), key=lambda j: u[j] * loads[j])
            tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
            movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                      or (include_offpref and cur[i] != pref_bay[i])]
            for i in movers:
                if not _within(sub_deadline):
                    break
                targets = sorted((j for j in range(m)
                                  if j != cur[i] and fits(blocks[i], bays[j])),
                                 key=lambda j: -blocks[i]["bay_preferences"][j])
                for j in targets:
                    trial = dict(cur)
                    trial[i] = j
                    tot, _ = total_obj(prob, trial, cache)
                    if tot < cur_tot - 1e-9:
                        cur, cur_tot = trial, tot
                        _, perbay = total_obj(prob, cur, cache)
                        improved = True
                        break

    hillclimb(False, _mid(deadline))
    hillclimb(True, deadline)
    return cur, cur_tot


def _climb(prob, assign, cache, deadline, patience=None):
    """Hill climb to convergence-or-deadline: move blocks in tardy bays (Z1), the
    max-(u*load) bay (Z2), or off their preferred bay (Z3); targets preference-first.
    With `patience` set (unified ILS loop), stop after that many CONSECUTIVE
    non-improving candidate evaluations (timing-independent convergence stop),
    `deadline` kept only as a hard safety cap. patience=None == legacy behaviour."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur = dict(assign)
    cur_tot, perbay = total_obj(prob, cur, cache)
    improved = True
    noimp = 0
    while improved and _within(deadline):
        improved = False
        loads = [0.0] * m
        for i, j in cur.items():
            loads[j] += blocks[i]["workload"]
        maxload = max(range(m), key=lambda j: u[j] * loads[j])
        tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
        movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                  or cur[i] != pref_bay[i]]
        for i in movers:
            if not _within(deadline):
                break
            targets = sorted((j for j in range(m)
                              if j != cur[i] and fits(blocks[i], bays[j])),
                             key=lambda j: -blocks[i]["bay_preferences"][j])
            for j in targets:
                trial = dict(cur)
                trial[i] = j
                tot, _ = total_obj(prob, trial, cache)
                if tot < cur_tot - 1e-9:
                    cur, cur_tot = trial, tot
                    _, perbay = total_obj(prob, cur, cache)
                    improved = True
                    noimp = 0
                    break
                elif patience is not None:
                    noimp += 1
                    if noimp >= patience:
                        return cur, cur_tot
    return cur, cur_tot


def _climb_lahc(prob, assign, cache, deadline, L, max_moves=None, rng=None):
    """Late-Acceptance Hill Climb -- LAHC at the MOVE level (its native granularity).
    Same neighborhood as _climb (relocate a block in a tardy [Z1] / max-(u*load) [Z2] /
    off-preferred [Z3] bay, targets tried preference-first), but a move is accepted when
    it beats the cost from L moves ago (`fa[v]`) OR the current cost -- so the DESCENT
    ITSELF can step uphill across a basin wall instead of halting at the first local
    optimum (which is what a strict hill climb does, and what makes an outer-loop LAHC
    degenerate here: deep climbs leave no `slightly-worse` gradations for late acceptance
    to exploit). Returns the running-MIN assignment seen along the walk, never the
    wandering current point -> Pareto-safe. L is the history length in MOVES.
    `total_obj` call sequence mirrors _climb (trial eval + a perbay refresh on accept).
    `rng` (restart diversity): when given, the mover order is shuffled each sweep, so two
    walks from the SAME a_pref seed diverge into different basins (targets stay
    preference-first, preserving per-step greedy quality). rng=None == deterministic."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur = dict(assign)
    cur_tot, perbay = total_obj(prob, cur, cache)
    best, best_tot = dict(cur), cur_tot
    Ln = max(1, L)
    fa = [cur_tot] * Ln
    it = 0
    # Mechanism-isolation control (SOLVER_LAHC_STRICT): accept ONLY strictly-improving
    # moves (no neutral, no uphill) -> pure greedy descent from the a_pref seed in this
    # loop structure. vs default (<=  + late-acceptance) it isolates the contribution of
    # neutral/uphill acceptance from the contribution of the a_pref fresh-descent itself.
    strict = _env_flag("SOLVER_LAHC_STRICT")
    while _within(deadline) and (max_moves is None or it < max_moves):
        loads = [0.0] * m
        for i, j in cur.items():
            loads[j] += blocks[i]["workload"]
        maxload = max(range(m), key=lambda j: u[j] * loads[j])
        tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
        movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                  or cur[i] != pref_bay[i]]
        if not movers:
            break
        if rng is not None:
            rng.shuffle(movers)   # restart-diversity: divergent trajectory per walk RNG
        moved = False
        for i in movers:
            if not _within(deadline) or (max_moves is not None and it >= max_moves):
                break
            targets = sorted((j for j in range(m)
                              if j != cur[i] and fits(blocks[i], bays[j])),
                             key=lambda j: -blocks[i]["bay_preferences"][j])
            for j in targets:
                trial = dict(cur)
                trial[i] = j
                tot, _ = total_obj(prob, trial, cache)
                v = it % Ln
                it += 1
                if strict:
                    accepted = tot < cur_tot - 1e-9
                else:
                    accepted = tot <= cur_tot + 1e-9 or tot <= fa[v] + 1e-9
                if accepted:
                    cur, cur_tot = trial, tot
                    _, perbay = total_obj(prob, cur, cache)
                    if cur_tot < best_tot - 1e-9:
                        best, best_tot = dict(cur), cur_tot
                    moved = True
                fa[v] = cur_tot
                if accepted:
                    break
        if not moved:
            break   # full sweep with no accepted move -> stuck plateau, stop the spin
    return best, best_tot


def _perturb(prob, best, cache, rng):
    """One ILS kick: re-home k in [2,5] blocks (random repair). With SOLVER_GUIDED,
    DESTROY contributing blocks -- in a tardy bay [Z1], the max-(u*load) bay [Z2], or
    off their preferred bay [Z3]; 'mix' uses guided on 50% of kicks. Returns the
    perturbed assignment. (rng call sequence identical to the previous inline version.)"""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    ids = list(best)
    k = rng.randint(2, 5)
    cand = dict(best)
    guided = os.environ.get("SOLVER_GUIDED")   # "1"=always guided, "mix"=50/50
    use_g = guided and (guided != "mix" or rng.random() < 0.5)
    if use_g:
        areas = [b["width"] * b["height"] for b in bays]
        u = [sum(areas) / m / a for a in areas]
        pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                    for i in range(len(blocks))}
        _, perbay = total_obj(prob, best, cache)
        loads = [0.0] * m
        for i, j in best.items():
            loads[j] += blocks[i]["workload"]
        maxload = max(range(m), key=lambda j: u[j] * loads[j])
        tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
        pool = [i for i in ids if best[i] in tardy or best[i] == maxload
                or best[i] != pref_bay[i]]
        chosen = rng.sample(pool, min(k, len(pool))) if pool else rng.sample(ids, min(k, len(ids)))
    else:
        chosen = [rng.choice(ids) for _ in range(k)]
    for i in chosen:
        opts = [j for j in range(m) if j != cand[i] and fits(blocks[i], bays[j])]
        if opts:
            cand[i] = rng.choice(opts)
    return cand


def _ils(prob, best, best_tot, cache, deadline, rng):
    """Iterated local search on the IDLE budget left after the main search converges:
    perturb the incumbent (see _perturb) and re-optimise (_climb), keeping the global
    best. SOLVER_GUIDED steers the perturbation; repair stays randomized for diversity."""
    while _within(deadline):
        cand = _perturb(prob, best, cache, rng)
        cur, tot = _climb(prob, cand, cache, deadline)
        if tot < best_tot - 1e-9:
            best, best_tot = cur, tot
    return best, best_tot


def _bestof_obj(prob, assign, deadline=None):
    """Full objective on the SAME per-bay model the final build uses (_score_and_pack):
    supercover-mask ONLY when SOLVER_MASK is on -- matching the search proxy exactly so
    `more search` can no longer drift the guard/build off what the search optimizes --
    else best-of(AABB, polygon). w1·ΣT + w2·Z2 + w3·Z3. Guards recombination adoption so
    it predicts the SHIPPED objective (Pareto-safe). Mask is ~15x cheaper than polygon."""
    w = prob["weights"]
    m = len(prob["bays"])
    mask_on = _env_flag("SOLVER_MASK") and _HAS_SHAPELY
    mask_R = int(os.environ.get("SOLVER_MASK_R", "8"))
    o1 = 0.0
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        if mask_on:
            # mask-only, identical to the search proxy and the final build (no AABB best-of)
            if _MULTIORDER:
                _, T, _ = solve_bay_best(prob, j, ids, mask=True, mask_R=mask_R, deadline=deadline)
            else:
                T, _ = extract_tardiness(prob, j, solve_bay(
                    prob, j, ids, mask=True, mask_R=mask_R, deadline=deadline))
        elif _MULTIORDER:
            _, Ta, _ = solve_bay_best(prob, j, ids, poly=False)
            _, Talt, _ = solve_bay_best(prob, j, ids, poly=True, deadline=deadline)
            T = min(Ta, Talt)
        else:
            Ta, _ = extract_tardiness(prob, j, solve_bay(prob, j, ids, poly=False))
            Talt, _ = extract_tardiness(prob, j, solve_bay(
                prob, j, ids, poly=True, deadline=deadline))
            T = min(Ta, Talt)
        o1 += T
    o2, o3 = obj23(prob, assign)
    return w["w1"] * o1 + w["w2"] * o2 + w["w3"] * o3


def _pool_prune_key(prob, T_index=2, ids_index=1):
    """Per-bay pool-prune sort key for a piece tuple c with c[0]=bay, c[ids_index]=ids,
    c[T_index]=tardiness. Keyed by the recombine COLUMN COST (w1*T + w3*pref_loss) by DEFAULT
    -- aligned with the (Z3-dominant) objective, so the prune keeps objective-relevant pieces
    (ties: wider coverage first), not merely
    low-tardiness ones. Validated: when the prune is tight, tardiness-only keeps low-T/bad-Z3
    pieces that won't tile, so recombine finds NO improvement, while cost-prune fires (prob_6
    -47%, prob_13 -25%; cost <= tardiness on every tested instance). SOLVER_POOL_PRUNE_COST=0
    restores the legacy tardiness key. When the pool is small enough that no prune happens
    (pool <= per_bay*m, e.g. the T=60 regime) both keys are bit-identical (no pieces dropped)."""
    if os.environ.get("SOLVER_POOL_PRUNE_COST", "1") == "0":
        return lambda c: (c[T_index], -len(c[ids_index]))
    w = prob["weights"]; blocks = prob["blocks"]
    w1, w3 = w["w1"], w["w3"]
    mx = [max(b["bay_preferences"]) for b in blocks]

    def key(c):
        j = c[0]; ids = c[ids_index]
        pl = 0
        for i in ids:
            pl += mx[i] - blocks[i]["bay_preferences"][j]
        return (w1 * c[T_index] + w3 * pl, -len(ids))
    return key


def _recombine(prob, best, deadline):
    """Z2-aware set-partitioning recombination of the cached (bay,set) pieces.
    Local search moves one block at a time and cannot recombine whole bay-pieces
    across solutions; the MIP can. column cost = w1·tardiness + w3·preference,
    global term = w2·Z2 (min-max, linearized). Hinted with the incumbent (so a
    time cut-off still yields >= incumbent on the MIP metric) and adopted only if
    the full best-of objective actually improves -- Pareto-safe."""
    if not _HAS_GUROBI:
        return best
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    n = len(blocks)
    w = prob["weights"]
    SCALE = 100
    cols = [(j, ids, T) for (j, ids), T in _POOL.items() if ids]
    if not cols:
        return best
    # Per-bay top-N prune (SOLVER_POOL_PER_BAY, default 1000; 0=off): bounds the MIP
    # model AND keeps it solvable within the 8s cap. The model-build and solve cost both
    # grow with the pool (one binary var + constraints per column) and are otherwise unbounded
    # as the search budget -- hence the pool -- grows: at long timelimits the uncapped MIP
    # could not be solved well in 8s, so recombine returned `best` unchanged and could not
    # rescue a poor incumbent (prob_20 E=10000: 1.87M uncapped vs 618k capped). Keep each
    # bay's lowest-tardiness pieces (ties: wider
    # coverage first) plus the incumbent's own pieces (so the hint stays valid and
    # `best` remains representable -> the guard can never adopt something worse). The
    # recombine benefit plateaus within a few thousand columns (see experiment board).
    _per_bay = int(os.environ.get("SOLVER_POOL_PER_BAY", "1000"))
    if _per_bay > 0 and len(cols) > _per_bay * m:
        inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
               for j in range(m) if any(best[i] == j for i in best)}
        # Prune key: by default keep each bay's lowest-TARDINESS pieces. The objective is
        # Z3-dominant, though, so tardiness-only keeps low-T pieces that may be bad on the
        # dominant w3*pref term and discards moderate-T/low-pref pieces the recombine needs
        # (this is what diluted the portfolio's cross-worker UNION -> recombine found no
        # improvement). SOLVER_POOL_PRUNE_COST switches the key to the piece's actual recombine
        # COLUMN COST (w1*T + w3*pref_loss), i.e. its real contribution to the objective.
        _key = _pool_prune_key(prob, T_index=2, ids_index=1)
        bins = [[] for _ in range(m)]
        for c in cols:
            bins[c[0]].append(c)
        cols = []
        for j in range(m):
            b = bins[j]
            b.sort(key=_key)
            keep = b[:_per_bay]
            kept = {(c[0], c[1]) for c in keep}
            keep.extend(c for c in b if (c[0], c[1]) in inc and (c[0], c[1]) not in kept)
            cols.extend(keep)
    model = _gp.Model(env=_grb_env())
    x = [model.addVar(vtype=_GRB.BINARY, name=f"c{k}") for k in range(len(cols))]
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
        model.addConstr(_gp.quicksum(x[k] for k in by_block[i]) == 1)
    for j in range(m):
        if by_bay[j]:
            model.addConstr(_gp.quicksum(x[k] for k in by_bay[j]) <= 1)
    obj = _gp.quicksum(cost[k] * x[k] for k in range(len(cols)))
    if m >= 2:
        areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
        cj = [round(avg / areas[j] * SCALE) for j in range(m)]
        sload = [_gp.quicksum(cj[j] * wl[k] * x[k] for k in by_bay[j]) for j in range(m)]
        M = model.addVar(lb=0, ub=10 ** 12, vtype=_GRB.INTEGER, name="M")  # = SCALE * Z2
        for a in range(m):
            for b in range(m):
                if a != b:
                    model.addConstr(M >= sload[a] - sload[b])
        obj = obj + w["w2"] * M
    model.setObjective(obj, _GRB.MINIMIZE)
    inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
           for j in range(m) if any(best[i] == j for i in best)}
    # MIP start = the incumbent (kept representable by the prune above), so a time
    # cut-off still returns a solution >= the incumbent on the MIP metric.
    for k, (j, ids, T) in enumerate(cols):
        x[k].Start = 1 if (j, ids) in inc else 0
    # The recombine objective fully plateaus by ~8s of MIP (measured: the set-
    # partitioning improvement appears at a threshold then is flat -- see
    # docs/experiment_board.md); the guard is now mask-cheap, so give the solve the
    # whole reserve up to that 8s cap (env-tunable). Previously max_time scaled with
    # the reserve (=timelimit*0.18*0.5), so at long timelimits it wasted 15-20s of
    # solver time for zero gain and inflated the idle tail.
    _solve_cap = float(os.environ.get("SOLVER_RECOMB_SOLVE_S", "8"))
    model.Params.TimeLimit = min(_solve_cap, max(0.5, deadline - time.time()))
    # default 4 threads; a parallel portfolio sets SOLVER_CP_WORKERS=1 so N chains use
    # N*1 cores (no >4-thread over-subscription under the 4-core cpulimit).
    model.Params.Threads = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
    # NoRel heuristic (Params.NoRelHeurTime, SOLVER_RECOMB_NOREL seconds; default OFF): finds
    # good feasible solutions WITHOUT the LP relaxation -- last year's winner's lever for big
    # (>10k var) set-partitioning MIPs. It only pays off on a LARGE/hard pool; the shipped
    # cost-pruned pool (<=1000/bay, <=~5k columns) already solves to optimality in ~2s, so NoRel
    # there only wastes solve time and a larger pool to feed it regressed prob_20 (+6.5% @T=180).
    # Kept as an off-by-default knob for a future large-MIP recombine; on the shipped config it
    # is bit-identical. (It DID help the cross-worker union -16% on prob_20, but that union was
    # rejected -- see memory/portfolio-recombine-degraded.md.)
    _nrt = float(os.environ.get("SOLVER_RECOMB_NOREL", "0"))
    if _nrt > 0:
        model.Params.NoRelHeurTime = min(_nrt, float(model.Params.TimeLimit))
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
    # best-of full-objective guard (Pareto-safe): adopt only if A truly improves on
    # the SAME best-of model the build ships (_bestof_obj mirrors _score_and_pack).
    if _bestof_obj(prob, A, deadline) < _bestof_obj(prob, best, deadline) - 1e-9:
        return A
    return best


# --------------------------------------------------------------------------- #
# solution assembly + top-level solve
# --------------------------------------------------------------------------- #
def _score_and_pack(prob, assign, poly_deadline=None):
    """Full objective AND the exact packed bays used for that score, in a single pass.
    Returns (total, packed) where packed = [(bay, placed, exits), ...].

    SOLVER_MASK (default): mask-ONLY -- score the same supercover-mask packing the search
    optimizes, so the build/score never disagrees with the search (kills the proxy seam
    that let `more search` drift the true objective). solve_bay(mask=True) always returns
    a complete feasible packing (mask-disjoint => polygon-disjoint; degrades per-block to
    AABB once `poly_deadline` passes), so this is feasible and time-bounded in one pass.
    Otherwise: best-of(AABB, polygon) -- always pack AABB, also escalate to polygon when
    there's time, keep whichever tardiness is lower. SOLVER_NOPOLY=1 forces AABB-only."""
    w = prob["weights"]
    m = len(prob["bays"])
    mask_on = _env_flag("SOLVER_MASK") and _HAS_SHAPELY
    mask_R = int(os.environ.get("SOLVER_MASK_R", "8"))
    obj1 = 0.0
    packed = []
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        if mask_on:
            # mask-only: same packing the search optimizes (no AABB best-of) -> no seam.
            if _MULTIORDER:
                placed, T_best, exits = solve_bay_best(
                    prob, j, ids, mask=True, mask_R=mask_R, deadline=poly_deadline)
            else:
                placed = solve_bay(prob, j, ids, mask=True, mask_R=mask_R, deadline=poly_deadline)
                T_best, exits = extract_tardiness(prob, j, placed)
        else:
            if _MULTIORDER:
                placed, T_best, exits = solve_bay_best(prob, j, ids, poly=False)
            else:
                placed = solve_bay(prob, j, ids, poly=False)
                T_best, exits = extract_tardiness(prob, j, placed)
            use_poly = ((poly_deadline is None or time.time() < poly_deadline)
                        and not _env_flag("SOLVER_NOPOLY"))
            if use_poly:
                if _MULTIORDER:
                    placed_p, T_poly, exits_p = solve_bay_best(
                        prob, j, ids, poly=True, deadline=poly_deadline)
                else:
                    placed_p = solve_bay(prob, j, ids, poly=True, deadline=poly_deadline)
                    T_poly, exits_p = extract_tardiness(prob, j, placed_p)
                if T_poly < T_best:
                    placed, exits, T_best = placed_p, exits_p, T_poly
        packed.append((j, placed, exits))
        obj1 += T_best
    obj2, obj3 = obj23(prob, assign)
    total = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    return total, packed


def _solution_from_packed(packed):
    """Assemble the submission operations from already-packed bays."""
    ops = {}
    for j, placed, exits in packed:
        for p in placed:
            en, ex = int(p["entry"]), int(exits[p["id"]])
            ops.setdefault(str(ex), []).append({"type": "EXIT", "block_id": p["id"], "bay_id": j})
            ops.setdefault(str(en), []).append({"type": "ENTRY", "block_id": p["id"], "bay_id": j,
                                                "x": int(p["x"]), "y": int(p["y"]), "orient_idx": p["o"]})
    for k in ops:
        ops[k].sort(key=lambda d: 0 if d["type"] == "EXIT" else 1)
    # time keys in sorted order: check_feasibility reconstructs in insertion order,
    # and an EXIT key before its ENTRY key would lose the exit_time.
    ops = {k: ops[k] for k in sorted(ops, key=int)}
    return {"operations": ops}


def build_solution(prob, assign, poly_deadline=None):
    _, packed = _score_and_pack(prob, assign, poly_deadline=poly_deadline)
    return _solution_from_packed(packed)


def _mip_repair(prob, mip_tl, repair_deadline):
    """Assignment-MIP propose + local-search repair -- a guarded best-of candidate.
    Gurobi minimizes w2*Z2 + w3*Z3 over block->bay assignments (fits-constrained): a
    low-preference-loss assignment the single-trajectory ILS can't reach from a_pref.
    That assignment usually carries tardiness (Z1>0, catastrophic given the large w1),
    so local_search repairs Z1 to ~0 while keeping the low Z3. Returns the repaired
    assignment or None. Validated -16..-62% on high-Z3 instances (prob_11/13/20),
    never worse via the final best-of guard. Mirrors last year's winner's MIP-centric
    formulation (the Z2 min-max term acts as a soft capacity)."""
    if not _HAS_GUROBI:
        return None
    blocks = prob["blocks"]
    bays = prob["bays"]
    w = prob["weights"]
    n = len(blocks)
    m = len(bays)
    try:
        areas = [b["width"] * b["height"] for b in bays]
        avg = sum(areas) / m
        u = [avg / areas[j] for j in range(m)]
        md = _gp.Model(env=_grb_env())
        x = [[md.addVar(vtype=_GRB.BINARY) for j in range(m)] for i in range(n)]
        for i in range(n):
            md.addConstr(_gp.quicksum(x[i][j] for j in range(m)) == 1)
            for j in range(m):
                if not fits(blocks[i], bays[j]):
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
        md.setObjective(w["w2"] * Mv + w["w3"] * pref, _GRB.MINIMIZE)
        md.Params.TimeLimit = max(0.5, mip_tl)
        md.Params.Threads = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
        md.optimize()
        if md.SolCount == 0:
            md.dispose()
            return None
        A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
        md.dispose()
    except Exception:
        return None
    try:
        A2, _ = local_search(prob, A, {}, repair_deadline)   # repair Z1 -> ~0, keep low Z3
        return A2
    except Exception:
        return A


def framework_solve(prob, timelimit, _return_assignment=False):
    """Time-managed solve. Reserves a build margin, splits the rest between the
    two-phase search and the focused search (best-of, shared cache), and always
    returns a feasible solution.

    _return_assignment (parallel-portfolio worker mode, SOLVER_PORTFOLIO harness):
    run search + recombine but return (best_assign, base_incumbent_assign) instead of
    materializing -- the master true-scores candidates from all workers on one
    consistent _score_and_pack basis. In this mode no final-build time is reserved
    (the worker emits nothing), so that budget goes to the search. Default False keeps
    the single-process path byte-identical."""
    global _EVALS, _EVAL_LIMIT, _TRACE_ON, _TRACE_T0, _TRACE_BEST
    _EVALS = 0
    _POOL.clear()
    # Packing caches are keyed by id(block_data); clear them per solve so a
    # multi-instance harness cannot reuse stale geometry from a previous problem.
    clear_packing_caches()
    t0 = time.time()
    _TRACE_ON = _env_flag("SOLVER_TRACE")
    if _TRACE_ON:
        _TRACE.clear()
        _TRACE_T0 = t0
        _TRACE_BEST = float("inf")
    cache = {}
    safety = max(2.0, timelimit * 0.06)
    recomb_on = _HAS_GUROBI and not _env_flag("SOLVER_NORECOMB")
    snapshot_guard = _env_flag("SOLVER_SNAPSHOT_GUARD") and not _return_assignment
    try:
        snapshot_cap = max(0, int(os.environ.get("SOLVER_SNAPSHOT_CAP", "2")))
    except ValueError:
        snapshot_cap = 2
    snapshots = []

    def remember_snapshot(assign):
        if not snapshot_guard or snapshot_cap <= 0 or assign is None:
            return
        if any(assign == s for s in snapshots):
            return
        snapshots.append(dict(assign))
        if len(snapshots) > snapshot_cap:
            snapshots.pop(0)

    # EVALUATION mode (reproducible, deterministic): stop the searches by candidate-
    # evaluation count, run the full polygon build, and let the harness report wall
    # time. Default (submission) mode is wall-clock time-managed.
    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    _EVAL_LIMIT = int(max_evals) if max_evals else None

    # best heuristic as a guaranteed feasible floor; also detect whether tardiness
    # is even in play (min obj1 over seeds).
    best_seed, bt, min_o1 = None, float("inf"), float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob)
        tot, perbay = total_obj(prob, a, cache)
        min_o1 = min(min_o1, sum(perbay.values()))
        if tot < bt:
            bt, best_seed = tot, a
    best, best_tot = best_seed, bt

    recomb_deadline = None
    if _EVAL_LIMIT is not None:
        # eval-count thresholds (cumulative): improved 40%, local 70%, ILS 100%.
        imp_dl, bas_dl, ils_dl = _EVAL_LIMIT * 0.4, _EVAL_LIMIT * 0.7, _EVAL_LIMIT
        poly_deadline = None    # full deterministic polygon build
        if recomb_on:
            recomb_deadline = None   # set just before the call (wall-clock cap)
    else:
        # The exact polygon final-build only recovers PACKING-driven tardiness, so
        # reserve time for it only when tardiness is in play; preference-only
        # instances (a seed already reaches obj1=0) give the budget to search.
        # Reserve fractions/floors are env-tunable for budget-allocation A/B (default
        # unchanged). Calibrated pre-speedup; the find_slot work made build/recombine
        # ~4x faster, so these may now over-reserve and starve the search. See
        # docs/experiment_log.md.
        poly_frac = float(os.environ.get("SOLVER_POLY_RESERVE", "0.30"))
        recomb_frac = float(os.environ.get("SOLVER_RECOMB_RESERVE", "0.18"))
        poly_floor = float(os.environ.get("SOLVER_POLY_FLOOR", "6.0"))
        recomb_floor = float(os.environ.get("SOLVER_RECOMB_FLOOR", "5.0"))
        recomb_cap = float(os.environ.get("SOLVER_RECOMB_CAP", "10.0"))
        poly_build_reserve = (max(poly_floor, timelimit * poly_frac) if min_o1 > 1e-9
                              else max(1.0, timelimit * 0.04))
        # Recombine reserve: ABSOLUTE-capped (default 10s), not a raw fraction. Cost is
        # MIP-hardness-bound (~8s CP-SAT plateau) + a now-mask-cheap guard/build (~1s),
        # so it does NOT scale with timelimit. The old fraction (timelimit*0.18) ballooned
        # to 54s at a 300s limit -- search stopped early to leave it, recombine used <1/3,
        # and the rest became idle tail. The cap fits the recombine on the largest pools
        # (measured ~8.8s total) while freeing the surplus back to search at long limits;
        # at the 60s default min(10.8,10)=10 is effectively unchanged. Floor protects short
        # limits. See docs/experiment_board.md.
        recombine_reserve = (min(recomb_cap, max(recomb_floor, timelimit * recomb_frac))
                             if recomb_on else 0.0)
        if _return_assignment:
            # portfolio worker: no final build here, so reserve nothing for it and give
            # that budget to the search (the master does the single final build).
            poly_build_reserve = 0.0
        if _env_flag("SOLVER_ADAPTIVE_RESERVE"):
            # Adaptive reserve (env-gated): the fixed POLY fraction above was calibrated
            # pre-speedup and over-reserves on fast builds -- it starves the ILS and
            # leaves a large idle tail (wall << timelimit at minute+ budgets). Size the
            # polygon-build reserve from the MEASURED cost of one best-of build of the
            # seed instead, capped by the fixed fraction (so never larger than the current
            # default -> downside bounded) and floored. Self-tunes to instance size: a
            # fast/small build frees the tail to the search, a slow/large build keeps
            # enough reserve to protect the final build. The probe build is wall-clock
            # work accounted for via `now` below.
            #
            # The RECOMBINE reserve is intentionally left at its fixed fraction: recombine
            # cost is driven by set-partitioning MIP hardness, not by build cost, so sizing
            # it from build_cost starved it and regressed recombine-dependent instances
            # (prob_11 +23.7% under build-cost recombine sizing -> -14.9% once kept fixed).
            poly_margin = float(os.environ.get("SOLVER_ADAPT_POLY_MARGIN", "2.0"))
            _tb = time.time()
            _score_and_pack(prob, best_seed, poly_deadline=t0 + timelimit - safety)
            build_cost = time.time() - _tb
            if min_o1 > 1e-9:
                poly_build_reserve = min(poly_build_reserve,
                                         max(poly_floor, build_cost * poly_margin))
            now = time.time()
            search_total = max(0.0, (timelimit - safety) - (now - t0)
                               - poly_build_reserve - recombine_reserve)
            imp_dl = now + search_total * 0.5
            bas_dl = ils_dl = now + search_total
            recomb_deadline = now + search_total + recombine_reserve
            poly_deadline = t0 + timelimit - safety
        else:
            search_total = max(0.0, timelimit - poly_build_reserve - recombine_reserve - safety)
            imp_dl = t0 + search_total * 0.5
            bas_dl = ils_dl = t0 + search_total
            recomb_deadline = t0 + search_total + recombine_reserve
            poly_deadline = t0 + timelimit - safety

    # MIP-propose + ILS-repair candidate (SOLVER_MIP_REPAIR, wall mode). FRONT-LOADED: a Gurobi
    # assignment MIP (min w2*Z2 + w3*Z3) proposes a low-preference-loss basin the single ILS
    # trajectory can't reach from a_pref, then local_search repairs its tardiness; carried to
    # the final best-of guard so it can only ever help. Serial by necessity -- the WLS license
    # is SINGLE-USE (one Gurobi process at a time), so it cannot overlap the search's recombine
    # (a concurrent sidecar makes recombine silently no-op -> prob_40 +44%). Front-loaded (not
    # tail-placed) because the search deadlines below are t0-based, so this costs a bounded slice
    # AND -- unlike a post-recombine tail slice -- it (a) still fires on the biggest winners
    # (prob_20), which have no idle tail for a tail slice, and (b) preserves the full final-build
    # margin (a tail slice overran prob_40). Cost is ~8s of search -> small regressions on
    # search-bound low-Z3 instances (prob_37 +10.9%), but the high-Z3 wins (-23..-48%) dominate.
    mip_cand = None
    if (_env_flag("SOLVER_MIP_REPAIR")
            and _EVAL_LIMIT is None and not _return_assignment and _HAS_GUROBI):
        _mb = min(float(os.environ.get("SOLVER_MIP_REPAIR_CAP", "8")), timelimit * 0.15)
        mip_cand = _mip_repair(prob, _mb * 0.5, time.time() + _mb)

    base_incumbent = None
    try:
        if _env_flag("SOLVER_UNIFIED_ILS"):
            # Unified ILS (env-gated): one loop replaces the improved->local->ILS
            # pipeline. First establish an incumbent (improved_search's own-seed climb
            # plus a local_search from the heuristic seed) within the opening
            # INIT_FRAC of the search window (capped in wall-clock mode so long
            # timelimits do not spend minutes re-converging the same incumbent),
            # snapshot it as the trusted base, then
            # spend the WHOLE remaining budget perturbing the global best and
            # re-climbing the kicked point with best-of(_climb [improved's Z1+Z2+Z3
            # strategy from a start], local_search [Z1-only from a start]). This fixes
            # the legacy split where ils_dl == end-of-search left ILS ~0s of budget
            # once local_search consumed the window. Each climb runs to
            # convergence-or-deadline, so kicks get enough time to settle.
            init_frac = float(os.environ.get("SOLVER_UNIFIED_INIT_FRAC", "0.4"))
            # LAHC re-seeds the search from the raw a_pref and ignores the init incumbent,
            # so the greedy init is pure floor/guard overhead for it. A smaller init
            # fraction hands the freed budget to the LAHC search -- decisive at short
            # timelimits (validated 5000-eval T13/T20: 0.6->0.2 gives LAHC another ~-20%;
            # improved_search still converges a usable base_incumbent floor in 0.2).
            if _env_flag("SOLVER_LAHC"):
                init_frac = float(os.environ.get("SOLVER_LAHC_INIT_FRAC", "0.2"))
            # _now() is unit-agnostic (wall seconds, or eval-count in SOLVER_MAX_EVALS
            # mode) and matches the unit of ils_dl, so the init split is correct in both.
            search_start = _now()
            loop_dl = ils_dl
            init_budget = max(0.0, loop_dl - search_start) * init_frac
            if _EVAL_LIMIT is None:
                init_cap = float(os.environ.get("SOLVER_UNIFIED_INIT_CAP", "45"))
                if init_cap > 0:
                    init_budget = min(init_budget, init_cap)
            init_dl = search_start + init_budget
            asg_imp, t_imp = improved_search(prob, cache, deadline=init_dl)
            if t_imp < best_tot:
                best, best_tot = asg_imp, t_imp
            asg_bas, t_bas = local_search(prob, best_seed, cache, deadline=init_dl)
            if t_bas < best_tot:
                best, best_tot = asg_bas, t_bas
            base_incumbent = dict(best)
            remember_snapshot(base_incumbent)
            rng = random.Random(int(os.environ.get("SOLVER_SEED", "0")))
            # Per-kick inner-climb stop: by default deadline-driven (loop_dl), but
            # SOLVER_UNIFIED_PATIENCE switches each climb to a timing-independent
            # "stop after K consecutive non-improving evals" rule (loop_dl stays as the
            # hard safety cap). Decouples per-kick effort from wall time -> less wall
            # variance + more kicks when climbs converge early.
            _pat = os.environ.get("SOLVER_UNIFIED_PATIENCE")
            patience = int(_pat) if _pat else None
            snap_next = init_dl
            snap_span = max(1e-9, loop_dl - init_dl)
            snap_step = snap_span / max(1, snapshot_cap)
            # Basin-escape lever (SOLVER_LAHC, default OFF -> bit-identical greedy path).
            # The kick still restarts from the global `best` (strong in this few-deep-kicks
            # regime -- a precious kick is never wasted from a worse point), but each kick
            # is re-optimised with a MOVE-level Late-Acceptance Hill Climb (_climb_lahc)
            # instead of a strict hill climb: the descent itself may step uphill across a
            # basin wall (accept a move that beats the cost L moves ago), reaching optima a
            # strict climb cannot. `best` stays a running min and the final true-objective
            # guard re-scores, so this is Pareto-safe -- only the trajectory changes. L is
            # the move-history length (env SOLVER_LAHC_L); one parameter, no temperature.
            lahc_on = _env_flag("SOLVER_LAHC")
            lahc_L = max(1, int(os.environ.get("SOLVER_LAHC_L", "1000")))
            # Per-walk move cap (0 -> one walk runs to the deadline). With a cap the loop
            # runs MULTIPLE bounded LAHC walks, each re-seeded from a kick of the best:
            # restart diversity (greedy's strength here) + per-walk basin-crossing (LAHC's).
            lahc_walk = int(os.environ.get("SOLVER_LAHC_WALK", "0")) or None
            # Restart diversity (SOLVER_LAHC_DIVERSE, default OFF -> kick re-seed below).
            # A walk halts when a full sweep accepts no move (stuck plateau), usually before
            # the deadline, so the loop fits several walks. The default re-seeds walks 2..N
            # from a kick of `best`; the a_pref-fresh-descent finding shows those kicks tend
            # to stall in the SAME basin. DIVERSE instead re-seeds EVERY walk from the raw
            # a_pref but shuffles its mover order from a per-walk RNG -> N independent diverse
            # descents into different basins, best-of keeping the running min (Pareto-safe).
            # Walk 0 stays unshuffled (= the proven deterministic descent), so DIVERSE is
            # strictly additive: it can only add best-of upside over the lone walk.
            lahc_diverse = _env_flag("SOLVER_LAHC_DIVERSE")
            lahc_base_seed = int(os.environ.get("SOLVER_SEED", "0"))
            lahc_started = False
            lahc_seed = a_pref(prob) if lahc_on else None
            walk_idx = 0
            while _within(loop_dl):
                if lahc_on:
                    # First walk starts from the RAW seed a_pref (high cost), NOT the
                    # converged incumbent: LAHC can only step uphill when its history `fa`
                    # still holds the higher costs from earlier in the descent, so seeding
                    # from a low-cost local optimum (tight fa) disables escapes entirely.
                    # A long descent from the raw seed keeps higher fa entries that let the
                    # walk cross basin walls. Subsequent walks re-seed per the policy above.
                    if lahc_diverse:
                        start = lahc_seed
                        wrng = (None if walk_idx == 0
                                else random.Random(lahc_base_seed * 1000003 + walk_idx))
                    else:
                        start = lahc_seed if not lahc_started else _perturb(prob, best, cache, rng)
                        wrng = None
                    lahc_started = True
                    walk_idx += 1
                    cc, ct = _climb_lahc(prob, start, cache, loop_dl, lahc_L,
                                         max_moves=lahc_walk, rng=wrng)
                    if ct < best_tot - 1e-9:
                        best, best_tot = cc, ct
                else:
                    kicked = _perturb(prob, best, cache, rng)
                    c1, t1 = _climb(prob, kicked, cache, loop_dl, patience=patience)
                    c2, t2 = local_search(prob, kicked, cache, loop_dl, patience=patience)
                    cc, ct = (c1, t1) if t1 <= t2 else (c2, t2)
                    if ct < best_tot - 1e-9:
                        best, best_tot = cc, ct
                if snapshot_guard and _now() >= snap_next:
                    remember_snapshot(best)
                    while _now() >= snap_next:
                        snap_next += snap_step
        else:
            asg_imp, t_imp = improved_search(prob, cache, deadline=imp_dl)
            if t_imp < best_tot:
                best, best_tot = asg_imp, t_imp
            asg_bas, t_bas = local_search(prob, best_seed, cache, deadline=bas_dl)
            if t_bas < best_tot:
                best, best_tot = asg_bas, t_bas
            # Snapshot the pre-ILS incumbent. The proxy-driven tail below (ILS, and the
            # recombination guarded only against ITS input) optimises the AABB proxy
            # (total_obj), but the submission is built on the best-of(AABB, polygon)
            # objective. A proxy gain can be a true-objective regression, so this trusted
            # incumbent anchors the final true-objective guard (after the searches).
            base_incumbent = dict(best)
            remember_snapshot(base_incumbent)
            # iterated local search on whatever budget the main search left unused
            # (SOLVER_NOILS=1 disables it -- ablation baseline)
            rng = random.Random(int(os.environ.get("SOLVER_SEED", "0")))
            do_ils = not _env_flag("SOLVER_NOILS")
            # H2 (search -> recombine -> search loop) was tested and rejected: splitting the
            # ILS budget to recombine mid-search commits the assignment to a basin built from
            # a thin pool, then burns the rest re-searching there. Deterministic E=2000 A/B
            # vs the single final recombine: prob_13 +9.9%, prob_17 +57.7%, prob_5 0% -- worse
            # everywhere, no winning instance. One uninterrupted ILS + one final recombine of
            # the rich pool is strictly better. See docs/experiment_log.md.
            if do_ils:
                best, best_tot = _ils(prob, best, best_tot, cache, deadline=ils_dl, rng=rng)
                remember_snapshot(best)
        # Z2-aware set-partitioning recombination of the cached pieces (guarded;
        # adopted only if the full best-of objective improves). SOLVER_NORECOMB=1
        # disables it. Shared by both search branches.
        if recomb_on:
            rdl = recomb_deadline if recomb_deadline is not None else (time.time() + 30.0)
            try:
                best = _recombine(prob, best, deadline=rdl)
            except Exception:
                # A local size-limited Gurobi license can reject large recombine models.
                # Keep the incumbent and still spend the remaining wall in idle ILS below.
                pass
        # Opportunistic idle reclaim (SOLVER_IDLE_ILS, wall mode only). After recombine,
        # spend any leftover wall on MORE guarded ILS from the current best. The init/ILS
        # deadlines are UNCHANGED, so this only EXTENDS the same trajectory (best is a
        # running min -> monotonic, can't regress) -- unlike shrinking the reserve, which
        # moves the init deadline and reshapes the trajectory (drift). Targets the dominant
        # Z3/Z2 assignment cost. idle_dl leaves a build margin for the final build.
        if (_env_flag("SOLVER_IDLE_ILS") and _EVAL_LIMIT is None
                and not _return_assignment):
            # Size the idle window from the ACTUAL build cost -- one throwaway build of the
            # current best (mask-only, ~1-3s) -- not the inflated pre-Gurobi a_pref reserve.
            # The margin must cover the final guard's builds (best, then the losing
            # base_incumbent) UNtruncated, else truncation -> AABB falls back -> adds Z1 on
            # build-sensitive instances and breaks monotonicity (observed prob_11 +9.9%).
            _t = time.time()
            _score_and_pack(prob, best, poly_deadline=poly_deadline)
            _build = time.time() - _t
            idle_dl = poly_deadline - max(0.5, _build
                                          * float(os.environ.get("SOLVER_IDLE_BUILD_MARGIN", "2.5")))
            _rng2 = random.Random(int(os.environ.get("SOLVER_SEED", "0")) + 7)
            while time.time() < idle_dl:
                kicked = _perturb(prob, best, cache, _rng2)
                c1, t1 = _climb(prob, kicked, cache, idle_dl)
                c2, t2 = local_search(prob, kicked, cache, idle_dl)
                cc, ct = (c1, t1) if t1 <= t2 else (c2, t2)
                if ct < best_tot - 1e-9:
                    best, best_tot = cc, ct
                    remember_snapshot(best)
    except Exception:
        pass  # keep the best feasible assignment found so far

    # Portfolio worker mode: return the search result (post-recombine best + the trusted
    # base incumbent) for the master to true-score against other workers. No build here.
    if _return_assignment:
        return best, base_incumbent

    # Final true-objective guard (Guarded methodology). The proxy-driven tail (ILS +
    # recombination) may have moved `best` off `base_incumbent` on the AABB proxy
    # only. Score BOTH on the real best-of objective with _score_and_pack and submit
    # whichever is truly better, reusing the winner's packing so it is emitted without
    # re-packing. `best` is scored first so the candidate we would have shipped keeps
    # the full polygon budget; base_incumbent is then scored under the same deadline,
    # so the guard overrides only when base_incumbent is genuinely better -- never a
    # regression vs the previous behaviour. Skipped when best == base_incumbent (the
    # common case: no proxy move stuck), keeping that path bit-identical.
    # Best-of over {best, base_incumbent, mip_cand} on the true objective. `best` is scored
    # FIRST (keeps the full polygon budget) and wins ties, so adding candidates can only
    # improve the emitted solution -- never a regression. mip_cand is the MIP-repair candidate
    # (None unless SOLVER_MIP_REPAIR in wall mode). When only `best` remains this is the
    # original bit-identical build_solution(best) fast path.
    cands = [best]
    if base_incumbent is not None and base_incumbent != best:
        cands.append(base_incumbent)
    if mip_cand is not None and mip_cand != best and mip_cand not in cands:
        cands.append(mip_cand)
    for snap in snapshots:
        if snap != best and snap not in cands:
            cands.append(snap)
    if len(cands) > 1:
        try:
            win_obj, win_packed = _score_and_pack(prob, cands[0], poly_deadline=poly_deadline)
            for c in cands[1:]:
                o, pk = _score_and_pack(prob, c, poly_deadline=poly_deadline)
                if o < win_obj - 1e-9:
                    win_obj, win_packed = o, pk
            return _solution_from_packed(win_packed)
        except Exception:
            pass  # any failure: fall through to the standard build of `best`

    return build_solution(prob, best, poly_deadline=poly_deadline)
