"""ACCORD -- the 7th OGC 2026 solver: per-block congestion-price consensus.

Paradigm (distinct from every existing solver):
    iterate { price-augmented assignment MIP  ->  true per-bay pack  ->
              per-block congestion-price update }
i.e. an ADMM / surrogate-Lagrangian / tatonnement loop between the two poles of the
problem -- the PREFERENCE-IDEAL polytope (Z2+Z3, solved EXACTLY and near-instantly by a
Gurobi assignment MIP) and the PACKABLE set (Z1, measured exactly by the per-bay packer).
The coupling is a per-(block,bay) price vector learned from real packing outcomes: a block
that came out tardy charges its current bay, so the next MIP re-routes exactly the blocks
that cause congestion, at exactly the preference cost that congestion is worth.

Why this exists (falsification chain, docs/pillar_design.md + memory):
  * BRIDGE/PRISM/STOW/WEAVE/FLUX/HELM all share one paradigm: trajectory search over full
    assignments (moves/anchors/populations) + PASSIVE set-partitioning recombination of
    search-visited pieces. The one global tool cannot GENERATE what search never visits.
  * PILLAR (column generation = active pricing of single bay-columns) was falsified
    cleanly: with m ~ 4-5 huge overlapping columns, priced columns STRAND (reduced cost
    -231k on a 199k objective, yet LP moves +0.00% -- no complement coverage exists).
    Lesson: global coordination here must exchange WHOLE assignments, not columns.
  * ACCORD probe (.claude/scratch/_accord_probe.py): the price loop beats the static
    lambda-anchor grid (= PRISM's anchor family, the no-feedback special case) by 3.9-4.8x
    at equal budget -- T13 955,949 -> 200,288, T20 2,103,329 -> 543,671, RAW MIP iterates
    with zero search/repair. Adaptive per-block prices >> any static global scalar.

Recent research applied: ADMM-as-MIP-heuristic (Boyd et al. MIQP-ADMM; 2024-25 MILP-ADMM
lines; Mix-CALADIN arXiv:2604.14897), surrogate Lagrangian relaxation for scheduling
(Bragin et al.), price smoothing/stabilization (Wentges/Pessoa; arXiv:2604.23889).

Reuses ONLY the validated packing/scoring kernel from BRIDGE (imported as `K`), exactly
like PRISM does: the packer is the oracle, the loop is new. No LAHC/ILS/recombine runs
inside ACCORD -- the price loop IS the search. Time-managed; always returns feasible.
"""
from __future__ import annotations
import os
import sys
import math
import time
import random

_BRIDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE_DIR not in sys.path:
    sys.path.append(_BRIDGE_DIR)          # append, not insert: see prism_engine.py note
import solver as K  # noqa: E402
from packing import solve_bay, solve_bay_best, extract_tardiness  # noqa: E402

_gp = getattr(K, "_gp", None)
_GRB = getattr(K, "_GRB", None)
_HAS_GUROBI = getattr(K, "_HAS_GUROBI", False)

# Diagnostics from the last accord_solve (trajectory, best iteration, price stats).
LAST_STATS: dict = {}


def _env_flag(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v not in ("", "0", "false", "False")


def _fenv(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# oracle: true per-bay pack of a full assignment (the same per-bay model the search
# engines score with: multi-order best-of + supercover mask), plus per-block tardiness
# (exited - due), which is the price-update signal.
# --------------------------------------------------------------------------- #
class _Oracle:
    def __init__(self, prob):
        self.prob = prob
        self.blocks = prob["blocks"]
        self.m = len(prob["bays"])
        self.w = prob["weights"]
        self.cache = {}          # (j, ids) -> (T, {i: tau_i})

    def pack_bay(self, j, ids):
        key = (j, ids)
        hit = self.cache.get(key)
        if hit is None:
            if K._MULTIORDER:
                placed, _, _ = solve_bay_best(self.prob, j, list(ids),
                                              mask=K._MASK_SEARCH, mask_R=K._MASK_R_SEARCH)
            else:
                placed = solve_bay(self.prob, j, list(ids),
                                   mask=K._MASK_SEARCH, mask_R=K._MASK_R_SEARCH)
            T, exited = extract_tardiness(self.prob, j, placed)
            tau = {i: max(0.0, exited[i] - self.blocks[i]["due_date"]) for i in ids}
            # blockers of each tardy block: bay-mates whose stay overlapped the tardy
            # block's WAIT window [release, entry) -- they occupied the space that
            # delayed its admission. Price-update signal for ACCORD_BLOCKERS.
            blockers = {}
            ent = {p["id"]: p["entry"] for p in placed}
            ext = {p["id"]: p["exit"] for p in placed}
            for i, t in tau.items():
                if t > 0:
                    rel = self.blocks[i]["release_time"]
                    bl = tuple(k for k in ids if k != i
                               and ent[k] < ent[i] and ext[k] > rel)
                    if bl:
                        blockers[i] = bl
            hit = (T, tau, blockers)
            self.cache[key] = hit
        return hit

    def eval(self, asg):
        """(true-model total obj, obj1, {i: tau_i}, {i: blockers}) of a full assignment."""
        obj1, tau, blockers = 0.0, {}, {}
        for j in range(self.m):
            ids = tuple(sorted(i for i in asg if asg[i] == j))
            if not ids:
                continue
            T, tj, bj = self.pack_bay(j, ids)
            obj1 += T
            tau.update(tj)
            blockers.update(bj)
        z2, z3 = K.obj23(self.prob, asg)
        tot = self.w["w1"] * obj1 + self.w["w2"] * z2 + self.w["w3"] * z3
        return tot, obj1, tau, blockers


# --------------------------------------------------------------------------- #
# price-augmented assignment MIP:  argmin lam*w2*Z2 + w3*Z3 + sum price[i,j]*x[i,j]
# (assignment + fits constraints; Z2 min-max linearized). Deterministic: Threads=1,
# Seed=0 (mirrors PRISM's anchor discipline -- these MIPs prove optimality in <<1s).
# --------------------------------------------------------------------------- #
def price_mip(prob, price, lam, time_limit, warm=None, prox=0.0, prox_ref=None):
    """prox > 0 adds the ADMM proximal stabilizer: `prox` objective units for every block
    moved away from `prox_ref` (limits mass migration; tatonnement overshoot damper)."""
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
        md.Params.TimeLimit = max(0.5, time_limit)
        md.Params.Threads = int(os.environ.get("ACCORD_MIP_THREADS", "1"))
        md.Params.Seed = 0
        x = [[md.addVar(vtype=_GRB.BINARY) for j in range(m)] for i in range(n)]
        for i in range(n):
            md.addConstr(_gp.quicksum(x[i][j] for j in range(m)) == 1)
            fit_any = any(K.fits(blocks[i], bays[j]) for j in range(m))
            if fit_any:                      # a nowhere-fits block must stay unconstrained
                for j in range(m):
                    if not K.fits(blocks[i], bays[j]):
                        md.addConstr(x[i][j] == 0)
        load = [_gp.quicksum(blocks[i]["workload"] * x[i][j] for i in range(n))
                for j in range(m)]
        Mv = md.addVar(lb=0)
        for a in range(m):
            for b in range(m):
                if a != b:
                    md.addConstr(Mv >= u[a] * load[a] - u[b] * load[b])
        pref = _gp.quicksum(
            (max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]) * x[i][j]
            for i in range(n) for j in range(m))
        pterm = _gp.quicksum(v * x[i][j] for (i, j), v in price.items() if v)
        obj = lam * w["w2"] * Mv + w["w3"] * pref + pterm
        if prox > 0 and prox_ref is not None:
            obj = obj + prox * _gp.quicksum(1 - x[i][prox_ref[i]]
                                            for i in range(n) if i in prox_ref)
        md.setObjective(obj, _GRB.MINIMIZE)
        if warm is not None:
            for i in range(n):
                for j in range(m):
                    x[i][j].Start = 1 if warm.get(i) == j else 0
        md.optimize()
        if md.SolCount == 0:
            md.dispose()
            return None
        A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
        md.dispose()
        return A
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# the ACCORD loop
# --------------------------------------------------------------------------- #
def accord_solve(prob, timelimit, _return_assignment=False):
    """Time-managed ACCORD solve. Runs the price-consensus loop until the search window
    closes, keeps the best true-model iterate (guarded by an a_pref floor so the output
    is never worse than the trivial preference build), then materializes it with the
    validated polygon-escalation builder. Always returns a feasible solution.

    Determinism: with ACCORD_ITERS set (fixed iteration budget) the loop is reproducible
    (single-thread seeded MIPs, deterministic packer, seeded jitter rng) -- the A/B analog
    of SOLVER_MAX_EVALS. Default (wall-driven) matches the deployed path.
    """
    K._EVALS = 0
    K._POOL.clear()
    K.clear_packing_caches()
    t0 = time.time()
    w = prob["weights"]
    rng = random.Random(int(os.environ.get("SOLVER_SEED", "20260704")))

    # Defaults = the T13@60 sweep winner (.claude/scratch/_acc2_*.log, 07-04): slow price
    # memory + small step + proximal damping. vs the probe's raw loop (decay .7 / rho 1 /
    # no prox = 156,974): d90r03 121,810 -> +prox2 112,234 (Z1=0), which BEATS the 4-core
    # PRISM+MO+SWAP champion single-run (114,810) on a single core. Blocker charging
    # (ACCORD_BLOCKERS) REGRESSED in its v1 form (152,325) -- off.
    lam = _fenv("ACCORD_LAM", 1.0)
    decay = _fenv("ACCORD_DECAY", 0.9)          # price memory (tatonnement stability)
    rho0 = _fenv("ACCORD_RHO", 0.3)             # step scale (x w1 per unit tardiness)
    stall_k = int(_fenv("ACCORD_STALL", 6))     # jitter after this many non-improving iters
    backtrack = _env_flag("ACCORD_BACKTRACK", True)
    prox = _fenv("ACCORD_PROX", 2.0)            # x w3: per-moved-block proximal cost
    blk_frac = _fenv("ACCORD_BLOCKERS", 0.0)    # charge blockers this fraction of the victim's charge
    mip_tl = _fenv("ACCORD_MIP_TL", 4.0)
    iters_cap = os.environ.get("ACCORD_ITERS")  # deterministic A/B budget (optional)
    iters_cap = int(iters_cap) if iters_cap else None

    oracle = _Oracle(prob)

    # budget layout (mirrors prism_solve): safety tail + polygon build reserve.
    safety = max(2.0, timelimit * 0.06)
    floor_asg = K.a_pref(prob)
    best, (best_tot, best_o1, _, _) = dict(floor_asg), oracle.eval(floor_asg)
    poly_floor = _fenv("SOLVER_POLY_FLOOR", 6.0)
    poly_frac = _fenv("SOLVER_POLY_RESERVE", 0.30)
    poly_deadline = t0 + timelimit - safety

    def _search_end(cur_o1):
        """DYNAMIC build reserve: the 30% polygon reserve is only needed while the
        incumbent packs tardy (poly escalation fires on tardy bays only); the a_pref
        floor is almost always tardy, so a STATIC reserve sized off it left ~28% of the
        budget idle (T=60 runs used 39s) while the eventual best had Z1=0 and built
        cheap. Re-size off the CURRENT best each iteration -- the loop keeps a monotonic
        best, so reclaiming the reserve into more iterations can never regress, and the
        moment the best goes tardy again the full reserve is restored."""
        if _return_assignment:
            return poly_deadline
        r = (max(poly_floor, timelimit * poly_frac) if cur_o1 > 1e-9
             else max(1.0, timelimit * 0.04))
        return poly_deadline - r

    search_end = _search_end(best_o1)

    price = {}                                  # sparse {(i, j): v}
    rho = rho0
    prev_tot = None
    warm = None
    stall = 0
    jits = 0                                    # consecutive jitters without a new best
    it = 0
    traj = []
    iter_cost = mip_tl * 0.3 + 0.5              # refined by measurement below
    while _HAS_GUROBI:
        if iters_cap is not None:
            if it >= iters_cap:
                break
        elif time.time() + iter_cost > search_end:
            break
        t_it = time.time()
        A = price_mip(prob, price, lam, min(mip_tl, max(0.5, search_end - time.time())),
                      warm=warm, prox=prox * w["w3"], prox_ref=warm)
        if A is None:
            break
        tot, o1, tau, blockers = oracle.eval(A)
        traj.append((round(tot, 1), round(o1, 1)))
        if tot < best_tot - 1e-9:
            best, best_tot, best_o1 = dict(A), tot, o1
            search_end = _search_end(best_o1)
            stall = 0
            jits = 0
        else:
            stall += 1
        # adaptive step (surrogate-subgradient flavor): cool down when the iterate
        # worsened -- the mass-migration overshoot seen in the probe -- warm up gently
        # when it improved.
        if prev_tot is not None:
            rho = max(0.1, rho * 0.6) if tot > prev_tot + 1e-9 else min(4.0, rho * 1.05)
        prev_tot = tot
        # tatonnement update: decay all prices, charge tardy blocks at their bay.
        if price:
            price = {k: v * decay for k, v in price.items() if v * decay > 1e-9}
        for i, t in tau.items():
            if t > 0:
                k = (i, A[i])
                price[k] = price.get(k, 0.0) + rho * w["w1"] * t
                if blk_frac > 0:
                    bl = blockers.get(i, ())
                    if bl:
                        share = blk_frac * rho * w["w1"] * t / len(bl)
                        for kk in bl:
                            kb = (kk, A[i])
                            price[kb] = price.get(kb, 0.0) + share
        # stall escape, two tiers. Tier 1 (DEFAULT): seeded multiplicative jitter on the
        # learned price structure (keeps WHICH blocks are priced, shakes HOW MUCH) + step
        # reset. Tier 2 (price-space restart KICK, env ACCORD_JITS>0 -- INSTANCE-SPLIT,
        # off by default): re-seed the prices as random evictions of a block sample from
        # the incumbent's bays. Measured: kick WINS the exploration-hungry T17 (-6.4%,
        # 63,162 -> 59,096) but REGRESSES T13@180 +19..24% (the accumulated congestion
        # map is the asset there; a stability-center variant lost BOTH) -- same pattern
        # as guided-destroy/div01, so it lives as a portfolio/env lever, not a default.
        # Exception: when the prices are EMPTY at a stall (Z1=0 spin -- nothing tardy,
        # everything decayed) the MIP argmin is FIXED and tier-1 has nothing to shake,
        # so the kick fires unconditionally (v1 idled there; any exploration > none).
        kicked = False
        if stall >= stall_k:
            jits += 1
            _jits_cap = int(_fenv("ACCORD_JITS", 0))
            if not price or (_jits_cap > 0 and jits >= _jits_cap):
                price = {}
                ids_all = sorted(best)
                for i in rng.sample(ids_all, max(3, len(ids_all) // 20)):
                    price[(i, best[i])] = rho0 * w["w1"] * rng.uniform(1.0, 5.0)
                kicked = True
                jits = 0
            else:
                price = {k: v * rng.uniform(0.4, 1.6) for k, v in price.items()}
            rho = rho0
            stall = 0
        # backtrack the MIP warm start to the incumbent after a blow-up iterate
        # (prices already charged, so the re-solve leaves the bad basin); a tier-2 kick
        # also re-converges FROM the incumbent (its prices evict from `best`'s bays).
        warm = dict(best) if (kicked or (backtrack and tot > 1.3 * best_tot)) else A
        it += 1
        iter_cost = max(iter_cost * 0.7 + (time.time() - t_it) * 0.3, 0.2)

    LAST_STATS.clear()
    LAST_STATS.update({"iters": it, "traj": traj, "best_tot": best_tot,
                       "best_o1": best_o1, "price_nnz": len(price),
                       "first_iter": None if not traj else traj[0]})
    if _return_assignment:
        return best, best_tot

    _, packed = K._score_and_pack(prob, best, poly_deadline=poly_deadline)
    return K._solution_from_packed(packed)
