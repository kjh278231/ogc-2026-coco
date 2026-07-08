"""FLUX -- a fifth OGC 2026 solver: CONGESTION-AWARE assignment.

Paradigm (distinct from the other four):
  * BRIDGE = single-trajectory ILS/LAHC(relocate) + swap + MIP-repair + column-recombine.
  * PRISM  = spectrum of PACKING-BLIND MIP anchors (argmin lam*w2*Z2 + w3*Z3) -> LAHC refine.
  * STOW   = packing-policy diversity portfolio.
  * WEAVE  = co-evolving population on assignment vectors (crossover + path-relinking + eject).
  * FLUX   = the FIRST solver to model PACKING CONGESTION at the ASSIGNMENT stage. Its anchors
             minimise w3*Z3 + w2*Z2 SUBJECT TO a spatial-temporal footprint capacity signal --
             so they are near-packable (low Z1) by construction, not repaired after the fact.

Why this shape (measured this session, docs/newsolver_flux_design.md):
  * The heavy >=300s instances (P4/P5/P6 zone) are Z1-DOMINATED (packing contention is 92-99.9%
    of the objective even at the load-balanced mip16 anchor) -- NOT Z3-dominated. Every existing
    solver's anchor is PACKING-BLIND (mip_anchor has no congestion term; its Z2/workload balance
    is only a crude congestion proxy). The true congestion driver is peak simultaneous FOOTPRINT-
    AREA over the demand window [release, due), which predicts the packer's per-bay tardiness
    almost perfectly (util<=~0.9 => Z1~0; util>1 => Z1 rises steeply). Constraining/penalising
    that signal in the assignment yields near-Z1=0 assignments directly.
  * Two congestion regimes (energetic-reasoning floor): SCHEDULABLE (mandatory area peak <=
    capacity -> Z1=0 reachable) vs IRREDUCIBLE (mandatory peak > capacity -> Z1 floored, no lever
    helps -- the likely reason P6 is 'frozen'). A SOFT overflow penalty handles both: it drives
    util<=kappa where feasible and minimises unavoidable overflow otherwise, trading against Z3.

Reuses ONLY the validated per-bay packing/scoring/LAHC/recombine primitives from BRIDGE (as K).
Time-managed; always returns a feasible solution. Env knobs: FLUX_* (below) + the SOLVER_* stack.
"""
from __future__ import annotations
import os
import sys
import time
import random

# Reuse the validated kernel exactly as PRISM/WEAVE do: append (not insert) the bridge dir so
# `import solver`/`packing`/`utils` resolve while FLUX's own modules (inserted at sys.path[0] by
# the caller) still win for shared names (portfolio, myalgorithm).
_BRIDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE_DIR not in sys.path:
    sys.path.append(_BRIDGE_DIR)
import solver as K          # noqa: E402  validated kernel (packing + score + LAHC + recombine)
from packing import orient_bbox  # noqa: E402

_gp = getattr(K, "_gp", None)
_GRB = getattr(K, "_GRB", None)
_HAS_GUROBI = getattr(K, "_HAS_GUROBI", False)

LAST_STATS: dict = {}


def _env_flag(name, default=False):
    return K._env_flag(name) if os.environ.get(name) is not None else default


# --------------------------------------------------------------------------- #
# Congestion signal (the FLUX core): per-bay peak demand-window footprint-area utilisation.
# Computed from (release, due, footprint-area) ONLY -- no packer, so it is cheap enough to embed
# in a MIP and to guide moves. Validated to predict packed per-bay tardiness (see design doc).
# --------------------------------------------------------------------------- #
def _footprint_area(prob, i):
    mnx, mny, mxx, mxy = orient_bbox(prob["blocks"][i], 0)
    return (mxx - mnx) * (mxy - mny)


def _bay_area(prob, j):
    b = prob["bays"][j]
    return b["width"] * b["height"]


def _peak_util(prob, j, ids):
    """max_t (sum of footprint area of blocks in bay j whose [release,due) covers t) / bay area."""
    if not ids:
        return 0.0
    ev = []
    for i in ids:
        b = prob["blocks"][i]
        r = b["release_time"]
        d = max(b["due_date"], r + b["processing_time"])
        a = _footprint_area(prob, i)
        ev.append((r, a))
        ev.append((d, -a))
    ev.sort()
    cur = pk = 0.0
    for _, dz in ev:
        cur += dz
        if cur > pk:
            pk = cur
    return pk / _bay_area(prob, j)


# --------------------------------------------------------------------------- #
# Anchor 1 (Gurobi-free): util-guided greedy congestion repair from a_pref.
# Move min-preference-loss blocks off the most over-utilised bay to a lower-util fitting bay
# until every bay <= kappa (or no helpful move). Guided ONLY by the cheap util signal (no packer
# in the loop). Measured: from a_pref this reaches the packing-blind mip16 anchor's objective or
# better, WITHOUT Gurobi (T14 -12% vs mip16) -> a license-free near-packable anchor for workers.
# --------------------------------------------------------------------------- #
def greedy_congestion_anchor(prob, kappa=0.9, base=None, max_moves=5000):
    m = len(prob["bays"])
    blocks = prob["blocks"]
    A = dict(base) if base is not None else K.a_pref(prob)
    for _ in range(max_moves):
        byb = {j: [i for i in A if A[i] == j] for j in range(m)}
        util = {j: _peak_util(prob, j, byb[j]) for j in range(m)}
        over = max(range(m), key=lambda j: util[j])
        if util[over] <= kappa:
            break
        best = None
        for i in byb[over]:
            p = blocks[i]["bay_preferences"]
            for j in range(m):
                if j == over or util[j] >= util[over] or not K.fits(blocks[i], prob["bays"][j]):
                    continue
                key = (p[over] - p[j], util[j])   # least preference loss, emptiest target
                if best is None or key < best[0]:
                    best = (key, i, j)
        if best is None:
            break
        A[best[1]] = best[2]
    return A


# --------------------------------------------------------------------------- #
# Anchor 2 (MIP): congestion-penalised / lexicographic assignment.
#   penalty:  argmin  w3*Z3 + lam*w2*Z2 + mu * sum_intervals sum_bays overflow_{j,k}
#   lex:      first minimise total overflow C*; then argmin w3*Z3 + lam*w2*Z2 s.t. overflow<=C*
# overflow_{j,k} = max(0, sum_{i: [release_i,due_i) covers interval k} area_i*x[i][j] - kappa*A_j).
# Only event-interval boundaries (block release/due) are needed (demand is piecewise-constant);
# intervals that cannot overflow even the largest bay are skipped -> the model stays small.
# --------------------------------------------------------------------------- #
def _build_congestion_model(prob, lam, kappa):
    blocks, bays, w = prob["blocks"], prob["bays"], prob["weights"]
    n, m = len(blocks), len(bays)
    Abay = [_bay_area(prob, j) for j in range(m)]
    avg = sum(Abay) / m
    u = [avg / Abay[j] for j in range(m)]
    barr = [_footprint_area(prob, i) for i in range(n)]
    rel = [b["release_time"] for b in blocks]
    due = [max(b["due_date"], b["release_time"] + b["processing_time"]) for b in blocks]
    md = _gp.Model(env=K._grb_env())
    md.Params.OutputFlag = 0
    md.Params.Threads = int(os.environ.get("FLUX_MIP_THREADS", "1"))
    md.Params.Seed = 0
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
    # Overflow constraints on the demand-window event intervals. Only the PEAK-congestion windows
    # matter (Z1 is driven by the peak), and a window whose block set is a superset of another's
    # dominates it, so: keep only overflow-capable windows, dedupe identical block sets, and cap to
    # the top-K busiest (by footprint area) -- this bounds the model build+solve on big instances
    # (n up to ~300 -> ~600 raw intervals) with negligible loss (FLUX_MAX_INTERVALS, default 64).
    times = sorted(set(rel) | set(due))
    maxA = max(Abay)
    cand = []
    seen_sets = set()
    for k in range(len(times) - 1):
        lo, hi = times[k], times[k + 1]
        Sk = tuple(i for i in range(n) if rel[i] <= lo and due[i] >= hi)
        if not Sk or Sk in seen_sets:
            continue
        area = sum(barr[i] for i in Sk)
        if area <= kappa * maxA:
            continue
        seen_sets.add(Sk)
        cand.append((area, Sk))
    cap = int(os.environ.get("FLUX_MAX_INTERVALS", "64"))
    cand.sort(key=lambda c: -c[0])
    cong = _gp.LinExpr()
    for _area, Sk in cand[:cap]:
        for j in range(m):
            ov = md.addVar(lb=0)
            md.addConstr(ov >= _gp.quicksum(barr[i] * x[i][j] for i in Sk) - kappa * Abay[j])
            cong += ov
    return md, x, pref, Mv, cong, n, m


def _extract_assign(md, x, n, m):
    if md.SolCount == 0:
        md.dispose()
        return None
    A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
    md.dispose()
    return A


def mip_congestion_anchor(prob, lam=16.0, kappa=0.9, mu=None, time_limit=6.0):
    """Congestion-aware MIP anchor. mu=None -> lexicographic (overflow then Z3/Z2); else penalty."""
    if not _HAS_GUROBI:
        return None
    try:
        md, x, pref, Mv, cong, n, m = _build_congestion_model(prob, lam, kappa)
        w = prob["weights"]
        has_cong = cong.size() > 0     # no overflow-capable interval -> non-congested instance
        if not has_cong:
            # nothing to de-congest -> plain preference-ideal (== packing-blind mip_anchor(lam))
            md.setObjective(w["w3"] * pref + lam * w["w2"] * Mv, _GRB.MINIMIZE)
            md.Params.TimeLimit = time_limit
            md.optimize()
        elif mu is None:
            md.setObjective(cong, _GRB.MINIMIZE)
            md.Params.TimeLimit = time_limit
            md.optimize()
            if md.SolCount == 0:
                md.dispose()
                return None
            md.addConstr(cong <= md.ObjVal + 1e-6)
            md.setObjective(w["w3"] * pref + lam * w["w2"] * Mv, _GRB.MINIMIZE)
            md.Params.TimeLimit = time_limit
            md.optimize()
        else:
            md.setObjective(w["w3"] * pref + lam * w["w2"] * Mv + mu * cong, _GRB.MINIMIZE)
            md.Params.TimeLimit = time_limit
            md.optimize()
        return _extract_assign(md, x, n, m)
    except Exception:
        return None


def mip_anchor(prob, lam=16.0, time_limit=6.0):
    """Packing-BLIND preference-ideal MIP anchor: argmin lam*w2*Z2 + w3*Z3 (no congestion term).
    This is PRISM's mip16 -- kept in the FLUX spectrum as the WORKLOAD-BALANCE ideal, because when
    Z1=0 is EASILY reachable (schedulable instances, e.g. T20) both anchors clear Z1 and then the
    workload-balanced assignment wins on Z2, where the congestion anchor's footprint-spreading
    over-imbalances (measured: T20 wall mip16 Z2=1470 << mipcong Z2=2356). The congestion anchors
    win the OTHER regime (Z1-hard, e.g. T13/T14), so the best-of over both covers both."""
    if not _HAS_GUROBI:
        return None
    try:
        blocks, bays, w = prob["blocks"], prob["bays"], prob["weights"]
        n, m = len(blocks), len(bays)
        Abay = [_bay_area(prob, j) for j in range(m)]
        avg = sum(Abay) / m
        u = [avg / Abay[j] for j in range(m)]
        md = _gp.Model(env=K._grb_env())
        md.Params.OutputFlag = 0
        md.Params.Threads = int(os.environ.get("FLUX_MIP_THREADS", "1"))
        md.Params.Seed = 0
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
        md.Params.TimeLimit = time_limit
        md.optimize()
        return _extract_assign(md, x, n, m)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Anchor spectrum. The FLUX thesis: congestion-aware anchors land in near-Z1=0 basins the
# packing-blind anchors miss. Diversity is still the point (best-of over the spectrum), but the
# spectrum is built around the congestion signal rather than the workload-balance one.
#   a_pref            -- guaranteed-feasible floor + the Z3-ideal (restart-diverse lottery ticket)
#   greedy{kappa}     -- Gurobi-FREE congestion repair (per-worker friendly: no single-use license)
#   mip16             -- PRISM's packing-blind workload-balance ideal (wins the Z1-easy regime)
#   mipcong           -- congestion-aware MIP anchor (lexicographic by default; the strongest, but
#                        needs the serial Gurobi license -> master-only in the portfolio)
# FLUX_ANCHORS overrides the set; FLUX_KAPPAS the greedy kappas; FLUX_MIP_MU the MIP penalty mu
# (empty -> lexicographic). want_mip=False (workers) drops the Gurobi anchor.
# --------------------------------------------------------------------------- #
def _greedy_kappas():
    raw = os.environ.get("FLUX_KAPPAS", "0.9")
    out = []
    for t in raw.split(","):
        t = t.strip()
        if t:
            try:
                out.append(float(t))
            except ValueError:
                pass
    return out or [0.9]


def _mip_mu():
    v = os.environ.get("FLUX_MIP_MU", "")
    if v.strip() == "":
        return None                      # lexicographic
    try:
        return float(v)
    except ValueError:
        return None


def _anchors(prob, mip_tl, want_mip=True):
    """Build the FLUX anchor spectrum (see module docstring). Default = {apref, greedy0.9, mip16,
    mipcong}: the congestion anchors (greedy/mipcong) win the Z1-hard regime, mip16 (workload-
    balance) wins the Z1-easy regime, apref is the Z3-ideal restart -- best-of covers all. mip16 +
    mipcong use the serial Gurobi license (master-only in the portfolio); greedy/apref are free."""
    want = os.environ.get("FLUX_ANCHORS", "apref,greedy,mip16,mipcong").split(",")
    want = [t.strip() for t in want if t.strip()]
    lam = float(os.environ.get("FLUX_LAM", "16"))
    anchors = []
    if "apref" in want:
        anchors.append(("apref", K.a_pref(prob)))
    if "greedy" in want:
        for kap in _greedy_kappas():
            anchors.append((f"greedy{kap:g}", greedy_congestion_anchor(prob, kappa=kap)))
    if "mip16" in want and want_mip and _HAS_GUROBI:
        A = mip_anchor(prob, lam=lam, time_limit=mip_tl)
        if A is not None:
            anchors.append(("mip16", A))
    if "mipcong" in want and want_mip and _HAS_GUROBI:
        A = mip_congestion_anchor(prob, lam=lam, kappa=_greedy_kappas()[0],
                                  mu=_mip_mu(), time_limit=mip_tl)
        if A is not None:
            anchors.append(("mipcong", A))
    if not anchors:                      # never leave the spectrum empty
        anchors.append(("apref", K.a_pref(prob)))
    return anchors


# --------------------------------------------------------------------------- #
# Refinement: identical shape to PRISM's memetic LAHC (reuse the validated kernel). Each anchor is
# refined into its own Z1=0 basin via LAHC descent + guided SWAP (+ ejection when enabled) wrapped
# in an ILS perturbation loop that spends the whole budget instead of idling after the first
# plateau. The only FLUX-specific thing is the anchor set above; the descent is BRIDGE's.
# --------------------------------------------------------------------------- #
def _refine(prob, anchor, cache, dl, L, rng=None):
    if rng is None:
        rng = random.Random(int(os.environ.get("SOLVER_SEED", "20260702")))
    A0 = dict(anchor)
    _swap = K._env_flag("SOLVER_SWAP")
    _eject = K._env_flag("SOLVER_EJECTION") and hasattr(K, "_ejection_refine")
    best, best_tot = K._climb_lahc(prob, A0, cache, dl, L, rng=rng)
    if _swap:
        best, best_tot = K._z3_refine(prob, best, cache, dl)
    if _eject:
        best, best_tot = K._ejection_refine(prob, best, cache, dl)
    while K._within(dl):
        cand = K._perturb(prob, best, cache, rng)
        cur, tot = K._climb_lahc(prob, cand, cache, dl, L, rng=rng)
        if _swap:
            cur, tot = K._z3_refine(prob, cur, cache, dl)
        if _eject:
            cur, tot = K._ejection_refine(prob, cur, cache, dl)
        if tot < best_tot - 1e-9:
            best, best_tot = cur, tot
    return best, best_tot


def refine_anchor(prob, anchor, timelimit, L=1, eval_limit=None, seed=None):
    """Refine ONE given anchor with LAHC+ILS; return (best, pool, tot). Portfolio worker entry:
    the master hands each worker a ready anchor (incl. Gurobi-free greedy ones) so the worker runs
    pure NORECOMB LAHC with no Gurobi contention. `seed` diversifies the ILS/mover-shuffle."""
    K._POOL.clear()
    K.clear_packing_caches()
    K._EVALS = 0
    K._EVAL_LIMIT = eval_limit
    cache = {}
    dl = eval_limit if eval_limit is not None else time.time() + max(0.5, timelimit)
    rng = random.Random(seed) if seed is not None else None
    A, tot = _refine(prob, anchor, cache, dl, L, rng=rng)
    return A, dict(K._POOL), tot


def flux_solve(prob, timelimit, _return_assignment=False):
    """Time-managed FLUX solve: build the congestion-aware anchor spectrum, refine each with
    LAHC(+swap) into its Z1=0 basin (shared cache + pool), best-of, guarded recombine, materialize.
    Always returns a feasible solution. Eval-count mode (SOLVER_MAX_EVALS) splits the budget across
    anchors for deterministic A/B. Mirrors prism_solve's budgeting exactly."""
    K._EVALS = 0
    K._POOL.clear()
    K.clear_packing_caches()
    t0 = time.time()
    cache = {}

    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    K._EVAL_LIMIT = int(max_evals) if max_evals else None
    L = int(os.environ.get("FLUX_LAHC_L", os.environ.get("SOLVER_LAHC_L", "1")))
    recomb_on = _HAS_GUROBI and not K._env_flag("SOLVER_NORECOMB")
    mip_tl = float(os.environ.get("FLUX_MIP_TL", "6.0"))
    anchors = _anchors(prob, mip_tl, want_mip=not _env_flag("FLUX_NO_MIP"))
    n_anchors = max(1, len(anchors))

    if K._EVAL_LIMIT is not None:
        per = [K._EVAL_LIMIT * (k + 1) / n_anchors for k in range(n_anchors)]
        poly_deadline = None
        recomb_deadline = None
    else:
        safety = max(2.0, timelimit * 0.06)
        min_o1 = float("inf")
        for _, a in anchors:
            _, pb = K.total_obj(prob, a, cache)
            min_o1 = min(min_o1, sum(pb.values()))
        poly_floor = float(os.environ.get("SOLVER_POLY_FLOOR", "6.0"))
        poly_frac = float(os.environ.get("SOLVER_POLY_RESERVE", "0.30"))
        poly_build_reserve = (max(poly_floor, timelimit * poly_frac) if min_o1 > 1e-9
                              else max(1.0, timelimit * 0.04))
        recomb_cap = float(os.environ.get("SOLVER_RECOMB_CAP", "10.0"))
        recomb_floor = float(os.environ.get("SOLVER_RECOMB_FLOOR", "5.0"))
        recombine_reserve = (min(recomb_cap, max(recomb_floor, timelimit * 0.18))
                             if recomb_on else 0.0)
        if _return_assignment:
            poly_build_reserve = 0.0
        elapsed = time.time() - t0
        search_total = max(0.5, (timelimit - safety) - elapsed
                           - poly_build_reserve - recombine_reserve)
        recomb_deadline = t0 + timelimit - safety - poly_build_reserve
        poly_deadline = t0 + timelimit - safety
        per = [time.time() + search_total * (k + 1) / n_anchors for k in range(n_anchors)]

    best, best_tot, best_name = None, float("inf"), None
    per_anchor = []
    for k, (name, a) in enumerate(anchors):
        dl = per[k]
        A, tot = _refine(prob, a, cache, dl, L)
        per_anchor.append((name, round(tot)))
        if tot < best_tot:
            best, best_tot, best_name = A, tot, name
    LAST_STATS.update({"per_anchor": per_anchor, "best_anchor": best_name, "n_anchors": n_anchors})

    if recomb_on and best is not None:
        if K._EVAL_LIMIT is not None:
            rdl = time.time() + float(os.environ.get("SOLVER_RECOMB_SOLVE_S", "8"))
        else:
            rdl = recomb_deadline
        try:
            A2 = K._recombine(prob, best, rdl)
            t2, _ = K.total_obj(prob, A2, cache)
            LAST_STATS["recomb_obj"] = round(t2)
            if t2 < best_tot - 1e-9:
                best, best_tot = A2, t2
                LAST_STATS["recomb_improved"] = True
        except Exception:
            pass
    LAST_STATS["final_obj"] = round(best_tot)

    if _return_assignment:
        return best, anchors[0][1]
    if K._EVAL_LIMIT is not None:
        _, packed = K._score_and_pack(prob, best, poly_deadline=None)
    else:
        _, packed = K._score_and_pack(prob, best, poly_deadline=poly_deadline)
    return K._solution_from_packed(packed)
