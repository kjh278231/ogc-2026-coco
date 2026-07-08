"""HELM -- a sixth OGC 2026 solver: INSTANCE-ADAPTIVE anchor-spectrum routing.

Paradigm (distinct from the other five):
  * BRIDGE = single-trajectory ILS/LAHC + swap + MIP-repair + column-recombine.
  * PRISM  = spectrum of packing-blind MIP/heuristic anchors -> LAHC refine -> best-of.
  * STOW   = packing-policy diversity portfolio.
  * WEAVE  = co-evolving population on assignment vectors (path-relinking + ejection).
  * FLUX   = congestion-aware anchors (peak demand-window footprint-area capacity).
  * HELM   = the ROUTER. Every mechanism above turned out instance-split (each wins some
             instances and loses others; 4 cores force a trade-off). HELM measures WHICH
             regime an instance is in -- from cheap signals computable in <0.5s without any
             packer run -- and steers the shared portfolio harness to the anchor spectrum
             that WON that regime in the measured head-to-heads.

Routing signals (validated: scratchpad/_router_features.py separates the measured
PRISM-wins {T4, T38} from the FLUX-wins {T1, T5, T11, T13, T14} on all 40 train instances):
  * max_util = max per-bay peak demand-window footprint-area utilisation at a_pref
    (FLUX's congestion signal; predicts packed per-bay tardiness: util<=0.9 -> Z1~0).
  * rho = energetic-reasoning floor: peak mandatory-part area / total capacity. rho > 1
    means Z1 has a physical floor NO assignment can remove (the frozen-P6 class).

Regimes -> spectra (each spectrum is exactly the config that won its regime):
  * LOW         (rho<=1, max_util<=1.2): congestion is nearly free at a_pref -> the game is
                a_pref-basin restart diversity + workload balance. Spectrum = PRISM's
                {pref, balanced, capped, mip16} (protects T4-type packing-sensitive: FLUX
                lost T4 +5.9% with only one a_pref restart).
  * CONGESTED   (rho<=1, 1.2<max_util<2.5): schedulable but congested -- congestion-aware
                anchors reach Z1=0 basins the packing-blind ones miss. Spectrum = FLUX's
                {apref, greedy0.9, mip16, mipcong} (T13 -12% wall, T11 -18%, T5 -10%,
                T14 -9% vs PRISM; band A/B 7W/2L -12.9%, both losses vanish at wall).
  * HEAVY       (rho<=1, max_util>=2.5): heavy-schedulable (T20-class = P4/P5 zone). The
                congestion spectrum is a bad lottery here (T20 stuck at Z1=1, +23%, in all
                three measured draws) -- route to the champion config (= PRISM spectrum
                AND seed base: deterministic parity with the grader-best submission).
  * IRREDUCIBLE (rho>1): Z1 floored -> keep the champion behaviour (PRISM spectrum, which
                holds the grader best on P6-type); overflow-minimising variants are a
                later iteration.

Reuses flux_engine (anchor generators + the shared LAHC/swap/ILS refine) which itself
reuses ONLY the validated BRIDGE kernel (as K). Time-managed; always feasible. Env knobs:
HELM_* (below) + the SOLVER_* stack.
"""
from __future__ import annotations
import os
import random
import sys
import time

# flux_engine supplies the anchor generators (greedy/mip/mipcong), the congestion signal,
# the shared _refine (LAHC+swap+ejection+ILS) and the kernel K. Resolve it whether we run
# from the repo (helm/ next to flux/) or from a flat submission zip (all files adjacent).
_HERE = os.path.dirname(os.path.abspath(__file__))
_FLUX_DIR = os.path.join(os.path.dirname(_HERE), "flux")
if os.path.isdir(_FLUX_DIR) and _FLUX_DIR not in sys.path:
    sys.path.append(_FLUX_DIR)
import flux_engine as F   # noqa: E402
K = F.K

_HAS_GUROBI = F._HAS_GUROBI

LAST_STATS: dict = {}


def _env_flag(name, default=False):
    return K._env_flag(name) if os.environ.get(name) is not None else default


# --------------------------------------------------------------------------- #
# Routing signals (data-only, <0.5s, no packer).
# --------------------------------------------------------------------------- #
def _mandatory_floor(prob):
    """Energetic-reasoning floor rho: peak over t of the areas of blocks whose MANDATORY
    window [due-proc, release+proc) covers t, divided by TOTAL bay capacity. rho > 1 means
    some Z1 is unavoidable for every assignment+packing (the frozen-P6 class)."""
    ev = []
    for i, b in enumerate(prob["blocks"]):
        r = b["release_time"]
        p = b["processing_time"]
        d = max(b["due_date"], r + p)
        lo, hi = d - p, r + p
        if lo < hi:
            a = F._footprint_area(prob, i)
            ev.append((lo, a))
            ev.append((hi, -a))
    ev.sort()
    cur = pk = 0.0
    for _, dz in ev:
        cur += dz
        if cur > pk:
            pk = cur
    cap = sum(F._bay_area(prob, j) for j in range(len(prob["bays"])))
    return pk / cap if cap else 0.0


def classify(prob):
    """Return (regime, features). Regime in {"low", "congested", "irreducible"}.
    HELM_FORCE_REGIME overrides (A/B + emergency rollback to a fixed spectrum)."""
    forced = os.environ.get("HELM_FORCE_REGIME", "").strip().lower()
    m = len(prob["bays"])
    A = K.a_pref(prob)
    byb = {j: [] for j in range(m)}
    for i, j in A.items():
        byb[j].append(i)
    max_util = max(F._peak_util(prob, j, byb[j]) for j in range(m))
    rho = _mandatory_floor(prob)
    feats = {"max_util": round(max_util, 3), "rho": round(rho, 3)}
    if forced in ("low", "heavy", "congested", "irreducible"):
        return forced, feats
    rho_hi = float(os.environ.get("HELM_RHO_HI", "1.0"))
    util_hi = float(os.environ.get("HELM_UTIL_HI", "1.2"))
    util_heavy = float(os.environ.get("HELM_UTIL_HEAVY", "2.5"))
    if rho > rho_hi:
        return "irreducible", feats
    if max_util <= util_hi:
        return "low", feats
    if max_util >= util_heavy:
        # HEAVY-schedulable (T20-class, the P4/P5 zone): the congestion spectrum is a bad
        # lottery here -- measured T20@180 three times (default / +ejection / greedy->capped),
        # every draw stuck at Z1=1 (140020-140095, +23% vs champion), while the champion
        # spectrum+seed reaches Z1=0 (113657). Route to the champion config (deterministic
        # parity); the only measured sacrifice is T19's eval-only -8.7% FLUX upside.
        return "heavy", feats
    return "congested", feats


# --------------------------------------------------------------------------- #
# Routed anchor spectrum. Each regime's spectrum is EXACTLY the measured winner's config
# (anchor set, order, MIP time budget, worker seed base) so HELM reproduces the winning
# engine's behaviour per regime instead of inventing a new blend.
# --------------------------------------------------------------------------- #
_SPECTRA = {
    # PRISM's shipped config D: heuristic trio + mip16 (grader best P1-P5 as PRISM+MO).
    "low":         ("pref,balanced,capped,mip16", 4.0, 20260629),
    "heavy":       ("pref,balanced,capped,mip16", 4.0, 20260629),
    "irreducible": ("pref,balanced,capped,mip16", 4.0, 20260629),
    # FLUX's validated spectrum: congestion anchors + the workload-balance ideal.
    "congested":   ("apref,greedy,mip16,mipcong", 6.0, 20260702),
}

_HEUR_FNS = {"pref": "a_pref", "apref": "a_pref",
             "balanced": "a_balanced_load", "capped": "a_pref_capped"}


def _spectrum(regime):
    spec, mip_tl, seed_base = _SPECTRA[regime]
    spec = os.environ.get(f"HELM_ANCHORS_{regime.upper()}", spec)
    return [t.strip() for t in spec.split(",") if t.strip()], mip_tl, seed_base


def _anchors(prob, regime, want_mip=True):
    """Build the routed anchor spectrum for `regime`. Returns (anchors, seed_base)."""
    names, mip_tl, seed_base = _spectrum(regime)
    mip_tl = float(os.environ.get("HELM_MIP_TL", mip_tl))
    lam = float(os.environ.get("FLUX_LAM", "16"))
    anchors = []
    for name in names:
        if name in _HEUR_FNS:
            anchors.append((name, getattr(K, _HEUR_FNS[name])(prob)))
        elif name == "greedy":
            for kap in F._greedy_kappas():
                anchors.append((f"greedy{kap:g}", F.greedy_congestion_anchor(prob, kappa=kap)))
        elif name == "mip16" and want_mip and _HAS_GUROBI:
            A = F.mip_anchor(prob, lam=lam, time_limit=mip_tl)
            if A is not None:
                anchors.append(("mip16", A))
        elif name == "mipcong" and want_mip and _HAS_GUROBI:
            A = F.mip_congestion_anchor(prob, lam=lam, kappa=F._greedy_kappas()[0],
                                        mu=F._mip_mu(), time_limit=mip_tl)
            if A is not None:
                anchors.append(("mipcong", A))
    if not anchors:                      # never leave the spectrum empty
        anchors.append(("pref", K.a_pref(prob)))
    return anchors, seed_base


def _route_env(regime):
    """Regime-scoped env routing beyond the anchor spectrum. IRREDUCIBLE: multi-order
    packing OFF -- when Z1 is floored (rho>1) MO's tardy-bay best-of gains nothing while
    costing 2-3x per eval; reclaiming the throughput lets every worker converge deeper.
    Measured (T38@300 wall, close-in-time): MO-off 63884676 vs champion PRISM+MO 67166362
    (-4.9%) vs HELM MO-on 67455677 -- ALL four MO-off workers beat every MO-on worker.
    Independently consistent with the grader: 0629 PRISM (pre-MO) still holds the P6 best
    (PRISM+MO regressed +0.2%). Gate HELM_IRRED_MO=1 keeps MO on. The sentinel restores
    MO for a later non-irreducible instance solved in the same process."""
    if regime == "irreducible" and os.environ.get("HELM_IRRED_MO", "0") in ("0", "", None):
        os.environ["SOLVER_MULTIORDER"] = "0"
        os.environ["HELM_MO_FLIPPED"] = "1"
    elif os.environ.pop("HELM_MO_FLIPPED", None):
        os.environ["SOLVER_MULTIORDER"] = "1"


# Portfolio worker entry: identical contract to flux/prism refine_anchor (the master hands
# each worker a ready anchor; the worker runs pure NORECOMB LAHC+ILS, no Gurobi).
refine_anchor = F.refine_anchor


def helm_solve(prob, timelimit, _return_assignment=False):
    """Serial HELM solve (also the portfolio fallback): classify -> routed anchor spectrum
    -> refine each into its basin (shared cache+pool) -> best-of -> guarded recombine ->
    materialize. Mirrors flux_solve/prism_solve budgeting exactly; eval-count mode
    (SOLVER_MAX_EVALS) splits the budget across anchors for deterministic A/B."""
    K._EVALS = 0
    K._POOL.clear()
    K.clear_packing_caches()
    t0 = time.time()
    cache = {}

    regime, feats = classify(prob)
    _route_env(regime)
    LAST_STATS.clear()
    LAST_STATS.update({"regime": regime, "feats": feats})

    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    K._EVAL_LIMIT = int(max_evals) if max_evals else None
    L = int(os.environ.get("HELM_LAHC_L", os.environ.get("SOLVER_LAHC_L", "1")))
    recomb_on = _HAS_GUROBI and not K._env_flag("SOLVER_NORECOMB")
    anchors, seed_base = _anchors(prob, regime, want_mip=not _env_flag("HELM_NO_MIP"))
    # SOLVER_SEED (when set) still wins, matching prism/flux serial behaviour; otherwise
    # each anchor gets a fresh Random(seed_base) exactly like the source engine's _refine
    # default -- but WITHOUT mutating os.environ (a multi-instance process would otherwise
    # carry the first regime's seed into later instances).
    seed = int(os.environ.get("SOLVER_SEED", seed_base))
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
        A, tot = F._refine(prob, a, cache, dl, L, rng=random.Random(seed))
        per_anchor.append((name, round(tot)))
        if tot < best_tot:
            best, best_tot, best_name = A, tot, name
    LAST_STATS.update({"per_anchor": per_anchor, "best_anchor": best_name,
                       "n_anchors": n_anchors})

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
