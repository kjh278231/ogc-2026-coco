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

# 4 deployable profiles (eval machine = 4 cores). Runtime knobs only; the seed adds
# diversity on top of the structural knob (guided / mixed-guided / late-perturb).
PROFILES = [
    {"SOLVER_SEED": "12346"},                                      # random-balanced
    {"SOLVER_SEED": "20265", "SOLVER_GUIDED": "1"},                # guided
    {"SOLVER_SEED": "28184", "SOLVER_GUIDED": "mix"},              # mixed-guided
    {"SOLVER_SEED": "36103", "SOLVER_UNIFIED_INIT_FRAC": "0.4"},   # late-perturb
]

# One CP-SAT/Gurobi core per worker (4 workers x 1 = 4 cores) and pinned native
# threadpools so the 4 workers do not oversubscribe the 4-core budget.
_WORKER_FIXED = {
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
        return {"best": best, "base": base, "elapsed": time.time() - t, "err": None}
    except Exception as e:                                  # pragma: no cover
        return {"best": None, "base": None, "elapsed": time.time() - t, "err": repr(e)}


def portfolio_solve(prob, timelimit):
    """Run the portfolio and return a solution dict. Falls back to single-process
    framework_solve on any multiprocessing failure or if no worker produced a result."""
    import solver

    try:
        n = int(os.environ.get("SOLVER_PORTFOLIO_WORKERS", "4"))
    except ValueError:
        n = 4
    n = max(1, min(n, len(PROFILES)))

    t0 = time.time()
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

    # No worker produced an assignment: spend the REMAINING budget on a normal solve.
    if not any(r.get("best") is not None for r in results):
        remaining = max(1.0, timelimit - (time.time() - t0))
        return solver.framework_solve(prob, remaining)

    # Candidate set: guaranteed seed + each worker's best, true-scored on one basis.
    cands = [a_pref] + [r["best"] for r in results if r.get("best") is not None]
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
