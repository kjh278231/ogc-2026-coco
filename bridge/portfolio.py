"""Parallel search portfolio (V1) -- env-gated by SOLVER_PORTFOLIO.

N diversified workers each run the full BRIDGE search + recombine in a separate
process (spawn); the master warms the numba disk-cache once, gathers every worker's
assignment, and true-scores [guaranteed seed + each worker's best] on ONE consistent
`solver._score_and_pack` basis, emitting the single best materialized solution.

Validated at T=300 (docs/parallel_search_portfolio_experiment_design.md, port_t300.log):
prob_11 -35%, prob_20 -28%, prob_19 -15%, prob_12 -14%, prob_6 -8%, all feasible, wall
within the limit. Diversity comes from supported runtime knobs only (SEED / GUIDED /
UNIFIED_INIT_FRAC); import-time gates (MASK_SEARCH / NUMBA / MASK_PREPARE) are inherited
from the master env at spawn.

Robustness: any multiprocessing failure (e.g. an evaluator that forbids subprocesses, or
a missing __main__ guard -> RuntimeError) is caught and the call degrades to a normal
single-process `framework_solve`, so enabling the gate can never do worse than default.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 4 deployable profiles (eval machine = 4 cores). The ANCHOR worker is the single-process
# default (LAHC-L1 WITH recombine), so the master best-of can never lose to single-process
# -> Pareto-safe. The other 3 are NORECOMB diversity (greedy, LAHC-L30, LAHC-L1-DIVERSE):
# they cover the per-instance-best strategy/L and feed the master union-recombine pool. Only
# the anchor recombines during the worker window (the single-use Gurobi license has no
# contention); the master recombine runs after gather. SOLVER_NORECOMB / SOLVER_LAHC here
# override the _WORKER_FIXED + inherited (myalgorithm setdefault) values. See
# memory/lahc-basin-escape-win.md, [[gurobi-single-use-license]].
#
# The 4th worker was LAHC-L1-init0.1 ("more search budget"); it is replaced by LAHC-L1-
# DIVERSE: every LAHC walk re-seeds from the raw a_pref with a per-walk shuffled mover order
# (restart diversity) instead of the anchor's kick-of-`best` ILS re-seed. The kick chain
# exploits a big-valley basin but FUNNELS on multi-basin/trap instances; the diverse restart
# reaches a different basin. Net is a real-path WIN but NOT Pareto-safe: a wall T=180
# portfolio A/B (tools/_portf_ab*.txt, old shipped vs this) gives T1 -45% (75080->41024 --
# the trap-escape recombine alone could not reach), T11 -2.8%, T6/T17/T18/T20 unchanged, but
# T13 +9.1% (161210->175818). The +9.1% is the cost of the 4-core cap: T13's win is a fragile
# union-recombine synergy over ALL FOUR original full-speed worker pools, so dropping ANY
# worker -- or a 5-worker oversubscribe that slows every pool -- breaks it back to 175818.
# i01 was the least-cost drop (its ONLY unique real-path contribution was that T13 pool;
# identical to the anchor everywhere else). The big T1 win dominates on the rank-based
# leaderboard and is expected to help the trap-like large instances (P4-P6) where the current
# LAHC portfolio is weakest. NB: the eval-count matrix (tools/_lahc_matrix.txt, NORECOMB)
# OVERSTATED the case -- it cannot see recombine's own trap-escape (e.g. T20) -- trust the wall A/B.
PROFILES = [
    {"SOLVER_SEED": "20265", "SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1",
     "SOLVER_NORECOMB": "0"},                                                       # ANCHOR: LAHC-L1 + recombine (== single-process)
    {"SOLVER_SEED": "12346", "SOLVER_LAHC": "0"},                                   # greedy NORECOMB (diversity; distinct column pool for union-recombine)
    {"SOLVER_SEED": "28184", "SOLVER_LAHC": "1", "SOLVER_LAHC_L": "30"},            # LAHC L30 NORECOMB (uphill subset: T6/T17/T18)
    {"SOLVER_SEED": "36103", "SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1",
     "SOLVER_LAHC_DIVERSE": "1", "SOLVER_LAHC_INIT_FRAC": "0.1"},                   # LAHC L1 DIVERSE NORECOMB (a_pref restart-diversity; escapes traps: T1 -49%, T20 -27%)
]

# Pinned native threadpools so the workers do not oversubscribe the core budget.
# SOLVER_NORECOMB=1: workers do NOT recombine -- the WLS Gurobi license is single-use
# ([[gurobi-single-use-license]]), so 4 concurrent worker recombines collide and only the
# one that grabs the license first succeeds (the other 3 silently fail and their LAHC search
# is left un-recombined -> weak). Instead the workers do pure LAHC search and the MASTER runs
# the single union-recombine over all their pools (one license use, no contention), so EVERY
# worker's pool feeds recombine.
_WORKER_FIXED = {
    "SOLVER_NORECOMB": "1",
    "SOLVER_CP_WORKERS": "1", "SOLVER_POOL_PER_BAY": "1000",
    "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1", "NUMBA_NUM_THREADS": "1",
}


def _worker(prob, worker_tl, env):
    """Spawn-safe top-level worker: run search+recombine, return the assignment only."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    for k, v in _WORKER_FIXED.items():
        os.environ[k] = v
    for k, v in env.items():
        os.environ[k] = v
    import solver
    t = time.time()
    try:
        best, base = solver.framework_solve(prob, worker_tl, _return_assignment=True)
        # return the worker's column POOL too, so the master can recombine over the
        # UNION of all workers' pieces (a partition no single worker reached).
        return {"best": best, "base": base, "pool": dict(solver._POOL),
                "elapsed": time.time() - t, "err": None}
    except Exception as e:                                  # pragma: no cover
        return {"best": None, "base": None, "pool": {}, "elapsed": time.time() - t, "err": repr(e)}


def _probe():                                              # spawn-safety probe target
    return 1


def portfolio_solve(prob, timelimit):
    """Run the portfolio and return a solution dict. Falls back to single-process
    framework_solve on any multiprocessing failure or if no worker produced a result."""
    import solver

    t0 = time.time()

    # SPAWN-SAFETY PROBE. multiprocessing 'spawn' re-imports the evaluator's entry module in
    # every child; if that entry is not under `if __name__ == "__main__"`, each child re-runs
    # algorithm() -> recursive spawns / daemonic errors -> the workers never deliver and the
    # run silently degrades. We cannot control the evaluator, so probe first: spawn ONE trivial
    # worker on a short timeout. If it does not return 1 quickly+cleanly, the environment is
    # spawn-unsafe -> run the FULL single-process search instead (no breakage, no wasted budget
    # beyond the short probe). When safe, the probe cost is ~1s.
    if os.environ.get("SOLVER_PORTFOLIO_PROBE", "1") not in ("0", "", None):
        try:
            import multiprocessing as _mp
            _ctx = _mp.get_context("spawn")
            with _ctx.Pool(1) as _pp:
                _r = _pp.apply_async(_probe)
                if _r.get(timeout=min(25.0, max(5.0, 0.15 * timelimit))) != 1:
                    raise RuntimeError("probe mismatch")
        except Exception:
            return solver.framework_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    try:
        n = int(os.environ.get("SOLVER_PORTFOLIO_WORKERS", "4"))
    except ValueError:
        n = 4
    n = max(1, min(n, len(PROFILES)))

    safety = max(2.0, 0.04 * timelimit)
    poly_dl = t0 + timelimit - safety

    # Warm the numba disk-cache (one build) + measure build cost BEFORE spawning, so the
    # workers load the compiled cache instead of a thundering-herd simultaneous compile.
    a_pref = solver.a_pref(prob)
    tb = time.time()
    solver._score_and_pack(prob, a_pref, poly_deadline=poly_dl)
    build_cost = time.time() - tb

    # Adaptive master reserve: the master true-scores a_pref + up to n worker bests, each
    # a full build. a_pref (worst-packed) overestimates real worker builds, so ~3 covers
    # a_pref + the (faster) worker builds. a_pref is scored first as a valid emission floor.
    final_guard = min(0.40 * timelimit, max(6.0, build_cost * 3.0))
    gather_dl = t0 + timelimit - safety - final_guard
    worker_tl = max(5.0, gather_dl - time.time())

    results = []
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        payloads = [(prob, worker_tl, env) for env in PROFILES[:n]]
        with ctx.Pool(n) as pool:
            asyncs = [pool.apply_async(_worker, p) for p in payloads]
            pending = list(asyncs)
            while pending and time.time() < gather_dl:
                for a in list(pending):
                    if a.ready():
                        try:
                            results.append(a.get())
                        except Exception as e:             # pragma: no cover
                            results.append({"best": None, "base": None,
                                            "elapsed": 0.0, "err": repr(e)})
                        pending.remove(a)
                if pending:
                    time.sleep(0.05)
            pool.terminate()
            pool.join()
    except Exception:                                      # pragma: no cover
        results = []   # mp unavailable/forbidden/bootstrap error -> single-process fallback

    if os.environ.get("SOLVER_PORTF_DEBUG"):
        import sys as _sys
        _sys.stderr.write("[PORTF] workers=%d worker_tl=%.1f build_cost=%.1f final_guard=%.1f\n" % (
            len(results), worker_tl, build_cost, final_guard))
        for i, r in enumerate(results):
            bo = "-"
            if r.get("best") is not None:
                try:
                    bo, _ = solver._score_and_pack(prob, r["best"], poly_deadline=poly_dl)
                    bo = "%.0f" % bo
                except Exception as _e:
                    bo = "score_err:%r" % _e
            _sys.stderr.write("[PORTF] w%d env=%s elapsed=%.1f poolsz=%d err=%s trueobj=%s\n" % (
                i, PROFILES[i], r.get("elapsed", -1), len(r.get("pool") or {}), r.get("err"), bo))

    # No worker produced an assignment: spend the REMAINING budget on a normal solve.
    if not any(r.get("best") is not None for r in results):
        remaining = max(1.0, timelimit - (time.time() - t0))
        return solver.framework_solve(prob, remaining)

    # Candidate set: guaranteed seed + each worker's best, true-scored on one basis.
    cands = [a_pref] + [r["best"] for r in results if r.get("best") is not None]

    # Master UNION-recombine: merge every worker's column pool and run ONE set-
    # partitioning recombine over the UNION (NoRel-assisted). A single search (and
    # each worker alone) cannot recombine bay-pieces across DIFFERENT workers'
    # solutions -- the union MIP can, reaching a partition none found alone (the
    # workers converge to the SAME basin on the grader, so this is the lever to
    # escape it). Additive best-of candidate -> Pareto-safe (never worse); spends
    # the budget the converged workers leave idle. Gated by SOLVER_PORTF_UNION_RECOMB.
    if os.environ.get("SOLVER_PORTF_UNION_RECOMB", "1") not in ("0", "", None):
        try:
            union = {}
            for r in results:
                if r.get("pool"):
                    union.update(r["pool"])
            rec_dl = poly_dl - max(2.0, build_cost * (len(cands) + 1))
            if union and rec_dl > time.time() + 3.0:
                solver._POOL.clear()
                solver._POOL.update(union)
                os.environ.setdefault("SOLVER_RECOMB_NOREL",
                                      os.environ.get("SOLVER_PORTF_UNION_NOREL", "15"))
                rec = solver._recombine(prob, a_pref, rec_dl)
                if rec is not None:
                    cands.append(rec)
        except Exception:                                  # pragma: no cover
            pass

    best_obj, best_packed = float("inf"), None
    seen = set()
    for asg in cands:
        key = frozenset(asg.items())
        if key in seen:
            continue
        seen.add(key)
        try:
            obj, packed = solver._score_and_pack(prob, asg, poly_deadline=poly_dl)
        except Exception:                                  # pragma: no cover
            continue
        if obj < best_obj:
            best_obj, best_packed = obj, packed

    if best_packed is None:                                # pragma: no cover
        remaining = max(1.0, timelimit - (time.time() - t0))
        return solver.framework_solve(prob, remaining)
    return solver._solution_from_packed(best_packed)
