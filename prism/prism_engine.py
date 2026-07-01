"""PRISM -- a third OGC 2026 solver, independent of BRIDGE's search.

Paradigm (distinct from BRIDGE/ILS-LAHC, ALNS/LNS, covering/set-partition):
    a SPECTRUM of preference-ideal MIP anchors -> memetic LAHC refinement of each
    -> best-of + guarded recombination.

Motivation (measured, see tools/_facet_*.py probes + docs/experiment_board.md):
  * A global assignment MIP `argmin (lam*w2*Z2 + w3*Z3)` reaches 5-30x lower Z3 than
    ILS, but ignores packing so it carries catastrophic Z1.
  * Driving that Z1 to 0 with cuts (global Benders) or MIP-per-move LNS FAILED -- the
    w1*Z1-dominant, packing-driven structure defeats MIP-centric repair.
  * What DID work: use the MIP solution as a SEED ("anchor") for the validated cheap
    LAHC descent, which repairs Z1 to ~0 while keeping the anchor's low Z3/Z2. Anchor-
    seeded LAHC beat a_pref-seeded LAHC by -6..-22% on the hardest packing + Z3-heavy
    instances (T13/T15/T17/T20), and lost on others -> a strong COMPLEMENTARY member.
  * PRISM turns that into an algorithm: it does not commit to one anchor. It sweeps a
    SPECTRUM of anchors spanning the preference<->balance trade-off (a_pref, the load-
    balanced/capped heuristics, and MIP anchors at several `lam`), refines each with
    LAHC into a distinct Z1=0 basin, and emits the best-of (+ a recombination over all
    the bay-pieces the spectrum generated). The diversity is the point -- best-of over
    the spectrum captures whichever basin wins per instance.

Reuses ONLY the validated per-bay packing kernel + search/score primitives from the
BRIDGE solver module (imported as `K`); the anchor-spectrum orchestration here is new.
Time-managed; always returns a feasible solution.
"""
from __future__ import annotations
import os
import sys
import time
import random

# Import the validated kernel (packing + scoring + LAHC descent + recombine) from the
# BRIDGE solver module. We add ONLY its directory to sys.path so `import solver` and its
# `import packing`/`import utils` resolve, exactly as the covering prototype reuses the
# baseline kernel. PRISM contributes the anchor spectrum + orchestration, not the kernel.
_BRIDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
# APPEND (not insert-at-0): the bridge kernel's own imports (`packing`, `utils`, `solver`)
# must resolve, but inserting bridge at sys.path[0] SHADOWS PRISM's own modules -- in
# particular `import portfolio`/`import myalgorithm` would silently resolve to BRIDGE's
# (bridge also has portfolio.py + myalgorithm.py), so prism/myalgorithm.py was running
# bridge/portfolio.py with a crippled env. Appending keeps PRISM's dir (inserted at 0 by
# the caller) ahead, so PRISM modules win and only the bridge-only names fall through.
if _BRIDGE_DIR not in sys.path:
    sys.path.append(_BRIDGE_DIR)
import solver as K  # noqa: E402

_gp = getattr(K, "_gp", None)
_GRB = getattr(K, "_GRB", None)
_HAS_GUROBI = getattr(K, "_HAS_GUROBI", False)

# Diagnostics from the last prism_solve (per-anchor objectives, which anchor won,
# recombine effect). Analysis only; never read by the solve path.
LAST_STATS: dict = {}


def _env_flag(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v not in ("", "0", "false", "False")


# Default anchor spectrum: lam multiplies the w2*Z2 term in the MIP anchor objective.
# lam=1 reproduces the true-weight preference ideal (a*); larger lam spreads workload
# more (less crowding -> easier to pack); the heuristic seeds anchor the other end.
def _lambdas():
    # Default = {16} (single high-lam MIP anchor). MINIMAL-DISRUPTION fix to the shipped portfolio:
    # the shipped lam=1 MIP anchor was the WEAKEST worker (it RAW-packs 50-79% worse than high lam,
    # and is the worst worker after repair on every probed instance); replacing JUST it with lam=16
    # unlocks the load-spread basin the heuristic anchors cannot reach on the Z3-heavy family.
    # Per-worker wall A/B (.claude/scratch/{cfg_D,old_diag}, deployed portfolio, true obj) vs the
    # shipped {pref,balanced,capped,mip1}: mip16 WINS T17 -8.6%, T7 -8.2%, T13 (-8..-32%, see noise
    # note), T18 ~-3%; the other 3 workers (pref/balanced/capped) are UNCHANGED so T14/T38(balanced),
    # T11/T20(pref) and the easy instances are preserved; only T4 +3.4% / T15 +5% regress (one fewer
    # a_pref restart-seed -- mip1 was an a_pref clone on the packing-sensitive instances). This beats
    # the wider {pref,balanced,mip8,mip16}, which gave up a 2nd a_pref seed and regressed T4 +15%.
    # ⚠ WALL-NOISE: portfolio workers run to a wall deadline, so refinement converges variably under
    # machine load -- the SAME mip16 worker gave T13 125,371 (x3) and 168,579 (x1). Trust DIRECTION
    # (mip16 reaches basins OLD's anchors can't), not single-run magnitudes; <~10% swings are noise.
    # mip16 solves to PROVEN optimality in <=0.5s at mip_tl=4 (Threads=1,Seed=0, deterministic).
    # Override PRISM_LAMBDAS (e.g. "8,16" for two MIP anchors); count-capacity MIP was falsified.
    raw = os.environ.get("PRISM_LAMBDAS", "16")
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(float(tok))
            except ValueError:
                pass
    return out or [1.0]


def mip_anchor(prob, lam, time_limit):
    """argmin (lam*w2*Z2 + w3*Z3) over block->bay assignments (fits-constrained), Z2
    min-max linearized. Returns an assignment dict or None. This is the preference
    ideal at a given balance pressure `lam`; it ignores packing (Z1), which the LAHC
    refinement repairs. Mirrors K._mip_repair's master but parametric in `lam`."""
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
        # Determinism: the assignment MIP solves to optimality in ~0.02s on all train
        # sizes (n<=300, m<=5), but parallel MIP is non-reproducible even with a fixed
        # seed (ties broken by thread race), which made PRISM non-deterministic. Single
        # thread + fixed seed -> one reproducible argmin (needed for eval-count A/B and a
        # stable submission). The solve is so fast this costs nothing.
        md.Params.Threads = int(os.environ.get("PRISM_MIP_THREADS", "1"))
        md.Params.Seed = 0
        # WorkLimit is a DETERMINISTIC resource cap (work units, not wall time): unlike
        # TimeLimit it stops at the same point regardless of machine load, so a slow anchor
        # (high lam) is reproducible for eval-count A/B. Off by default (lam=1 finishes long
        # before any cap); set PRISM_MIP_WORKLIMIT for the spectrum experiment.
        _wl = os.environ.get("PRISM_MIP_WORKLIMIT")
        if _wl:
            md.Params.WorkLimit = float(_wl)
        md.optimize()
        if md.SolCount == 0:
            md.dispose()
            return None
        A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
        md.dispose()
        return A
    except Exception:
        return None


_HEUR_FNS = {"pref": "a_pref", "balanced": "a_balanced_load", "capped": "a_pref_capped"}


def _anchors(prob, mip_tl, want_mip=True):
    """Build the anchor spectrum: heuristic seed(s) + MIP preference-ideal spectrum.

    Default heuristic anchors = {a_pref, a_balanced_load, a_pref_capped} -- the SHIPPED trio, kept
    intact. `capped` ~= `a_pref` so the two act as restart-diverse lottery tickets on the a_pref
    basin (which the packing-sensitive instances T4/T7 win on); `balanced` refines into the winning
    basin on the heavy load-spread family (T38, T14 -- the >=300s P4/P5 zone). With lam={16} (see
    _lambdas) the 4 portfolio workers are {pref, balanced, capped, mip16}: only the shipped lam=1 MIP
    worker is replaced by mip16, the minimal change that adds the Z3-heavy basin without disturbing
    the heuristic coverage. Roll back / experiment via PRISM_HEUR_ANCHORS (subset of pref,balanced,
    capped)."""
    heur = os.environ.get("PRISM_HEUR_ANCHORS", "pref,balanced,capped")
    anchors = []
    for name in (t.strip() for t in heur.split(",")):
        fn = _HEUR_FNS.get(name)
        if fn:
            anchors.append((name, getattr(K, fn)(prob)))
    if not anchors:                       # never leave the portfolio without a heuristic floor
        anchors.append(("pref", K.a_pref(prob)))
    if want_mip and _HAS_GUROBI:
        for lam in _lambdas():
            A = mip_anchor(prob, lam, mip_tl)
            if A is not None:
                anchors.append((f"mip{lam:g}", A))
    return anchors


def _refine(prob, anchor, cache, dl, L, rng=None):
    """Refine one anchor into a Z1=0 basin, using the FULL budget `dl`.

    A single LAHC descent plateaus (`_climb_lahc` breaks on a no-improve sweep) long
    before the deadline -- in the wall portfolio the workers were returning in 5-50s of a
    180s budget and lost everywhere. So this wraps the descent in an ILS perturbation loop
    (BRIDGE's _ils shape, but LAHC descent + anchor seed): plateau -> _perturb (re-home a
    few blocks) -> re-LAHC -> keep the running best, until `dl`. This is what lets PRISM's
    anchor diversity actually pay off at the deployed budget.

    Env PRISM_REPAIR_FIRST (default off): a conditional, budget-capped tardy-bay Z1 repair
    before the first descent (the uncapped/unconditional version regressed -- see log)."""
    if rng is None:
        rng = random.Random(int(os.environ.get("SOLVER_SEED", "20260629")))
    A0 = dict(anchor)
    if _env_flag("PRISM_REPAIR_FIRST", False):
        try:
            _, perbay = K.total_obj(prob, A0, cache)
            if sum(perbay.values()) > 1e-9:
                frac = float(os.environ.get("PRISM_REPAIR_FRAC", "0.3"))
                if K._EVAL_LIMIT is not None:
                    cap = K._EVALS + max(1.0, frac * (dl - K._EVALS))
                else:
                    cap = time.time() + max(0.5, frac * (dl - time.time()))
                A0, _ = K.local_search(prob, A0, cache, cap)
        except Exception:
            A0 = dict(anchor)
    # rng drives BOTH the ILS kick AND (via _climb_lahc rng=) the per-sweep mover shuffle,
    # so different worker seeds diverge into different basins (restart diversity). This is
    # what lets the portfolio escape deterministic traps (e.g. T1, where one shared seed
    # left every worker stuck in the same Z1=1 basin -> +167%); BRIDGE gets this from div01.
    # Z1=0 phase-transition Z3 refinement (SOLVER_SWAP): after each LAHC descent reaches its
    # (Z1=0) local optimum, run guided SWAP moves to drive Z3 down further while preserving Z1=0
    # -- reaching mutual-preference exchanges relocation+recombine miss (K._z3_refine). Converges
    # fast (few swaps) so it does not hog the ILS budget; Pareto-safe.
    _swap = K._env_flag("SOLVER_SWAP")
    best, best_tot = K._climb_lahc(prob, A0, cache, dl, L, rng=rng)
    if _swap:
        best, best_tot = K._z3_refine(prob, best, cache, dl)
    # ILS loop: spend the rest of the budget instead of idling after the first plateau.
    while K._within(dl):
        cand = K._perturb(prob, best, cache, rng)
        cur, tot = K._climb_lahc(prob, cand, cache, dl, L, rng=rng)
        if _swap:
            cur, tot = K._z3_refine(prob, cur, cache, dl)
        if tot < best_tot - 1e-9:
            best, best_tot = cur, tot
    return best, best_tot


def refine_anchor(prob, anchor, timelimit, L=1, eval_limit=None, seed=None):
    """Refine ONE given anchor assignment with LAHC+ILS and return (best, pool, tot).
    Used by the parallel portfolio worker: the master computes the (Gurobi) anchors ONCE
    (single-use WLS license) and hands each worker a ready anchor, so the worker runs pure
    NORECOMB LAHC with no Gurobi contention. `seed` diversifies the ILS/mover-shuffle per
    worker (restart diversity)."""
    K._POOL.clear()
    K.clear_packing_caches()
    K._EVALS = 0
    K._EVAL_LIMIT = eval_limit
    cache = {}
    if eval_limit is not None:
        dl = eval_limit
    else:
        dl = time.time() + max(0.5, timelimit)
    rng = random.Random(seed) if seed is not None else None
    A, tot = _refine(prob, anchor, cache, dl, L, rng=rng)
    return A, dict(K._POOL), tot


def prism_solve(prob, timelimit, _return_assignment=False):
    """Time-managed PRISM solve. Refines each anchor with LAHC (shared packing cache),
    keeps the best-of, recombines the generated bay-pieces, and materializes the best.
    Always returns a feasible solution.

    Eval-count mode (SOLVER_MAX_EVALS=E, for deterministic A/B): the E-eval budget is
    split evenly across anchors; the MIP anchor solves do not consume evals.
    """
    K._EVALS = 0
    K._POOL.clear()
    K.clear_packing_caches()
    t0 = time.time()
    cache = {}
    w = prob["weights"]

    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    K._EVAL_LIMIT = int(max_evals) if max_evals else None

    L = int(os.environ.get("PRISM_LAHC_L", os.environ.get("SOLVER_LAHC_L", "1")))
    recomb_on = _HAS_GUROBI and not _env_flag("SOLVER_NORECOMB")

    # MIP anchor time budget: small absolute slice (assignment MIPs solve fast).
    mip_tl = float(os.environ.get("PRISM_MIP_TL", "4.0"))
    anchors = _anchors(prob, mip_tl, want_mip=not _env_flag("PRISM_NO_MIP"))
    n_anchors = max(1, len(anchors))

    # time / eval budgeting
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
        search_end = time.time() + search_total
        recomb_deadline = t0 + timelimit - safety - poly_build_reserve
        poly_deadline = t0 + timelimit - safety
        # per-anchor wall deadlines (even split of the search window)
        per = [time.time() + search_total * (k + 1) / n_anchors for k in range(n_anchors)]

    # PRISM_ANCHOR_FULL_BUDGET (eval-mode analysis): each anchor's LAHC gets the FULL
    # eval budget on its own counter, modelling the parallel deployment (one anchor per
    # core, each a full-budget worker) instead of the conservative even split. Total work
    # = n_anchors*E, but on n_anchors cores that is ~E wall. Off by default.
    full_budget = _env_flag("PRISM_ANCHOR_FULL_BUDGET") and K._EVAL_LIMIT is not None

    # refine each anchor with LAHC into its own Z1=0 basin (shared cache + pool)
    best, best_tot, best_name = None, float("inf"), None
    per_anchor = []
    for k, (name, a) in enumerate(anchors):
        if full_budget:
            K._EVALS = 0
            dl = K._EVAL_LIMIT
        else:
            dl = per[k]
        A, tot = _refine(prob, a, cache, dl, L)
        per_anchor.append((name, round(tot)))
        if tot < best_tot:
            best, best_tot, best_name = A, tot, name
    LAST_STATS.update({"per_anchor": per_anchor, "best_anchor": best_name,
                       "n_anchors": n_anchors})

    # guarded recombination over every (bay,set) piece the spectrum generated
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

    # materialize the best assignment with the polygon-escalation build budget
    if K._EVAL_LIMIT is not None:
        _, packed = K._score_and_pack(prob, best, poly_deadline=None)
    else:
        _, packed = K._score_and_pack(prob, best, poly_deadline=poly_deadline)
    return K._solution_from_packed(packed)
