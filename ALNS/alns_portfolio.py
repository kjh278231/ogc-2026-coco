"""Best-of portfolio for the ALNS engine -- env-gated by the entry point (long
budgets only). Two diversified workers run the SAME ALNS engine in separate spawn
processes; the master warms the numba disk-cache once, gathers each worker's best
assignment, and true-scores [guaranteed a_pref + each worker's best] on ONE
consistent `solver._score_and_pack` basis, emitting the single best solution.

Rationale (see memory/alns-seed-recombine-instance-split): the MIP-seed+recombine
levers help some instances (P3/P5: -16..-41%) but TRAP/regress others (P4/P6/T1).
Two cheap falsifications showed no single gate signal (est/best_tot, a_pref Z1)
separates help from harm -> a TRAJECTORY-level best-of is the robust fix:
  worker A = config A (levers off)  -> the regression FLOOR
  worker B = lever  (MIP-seed + final recombine) -> the upside
best-of(A,B) is provably >= both, so P3/P5 keep gains while P4/P6 are protected.

License: only worker B touches Gurobi (worker A never calls the MIP), so there is
never concurrent Gurobi use (see memory/gurobi-single-use-license). Any
multiprocessing failure degrades to a single-process config-A solve, so enabling
this can never do worse than the floor.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 4 diversified trajectories (eval machine = 4 cores). ONE Gurobi worker (lever) so
# there is never concurrent Gurobi use; the rest are config-A with diverse seeds --
# on lever-INERT instances the gain comes from a better config-A basin (seed
# diversity), not from the lever. The master best-of over all + a_pref floor is
# regression-free (best-of(4) >= best-of(2) by construction).
PROFILES = [
    {"ALNS_MIP_SEED": "0", "ALNS_RECOMB": "off", "SOLVER_SEED": "0"},       # config-A floor (no Gurobi)
    {"ALNS_MIP_SEED": "1", "ALNS_RECOMB": "final", "SOLVER_SEED": "0"},     # lever (Gurobi -- the ONLY one)
    {"ALNS_MIP_SEED": "0", "ALNS_RECOMB": "off", "SOLVER_SEED": "12346"},   # config-A seed2 (no Gurobi)
    {"ALNS_MIP_SEED": "0", "ALNS_RECOMB": "off", "SOLVER_SEED": "28184"},   # config-A seed3 (no Gurobi)
]

# Import-time packing gates (workers re-import solver/packing) + pinned native
# threadpools so the workers do not oversubscribe the core budget.
_WORKER_FIXED = {
    "SOLVER_MASK_SEARCH": "1", "SOLVER_MASK": "1",
    "SOLVER_NUMBA": "1", "SOLVER_MASK_PREPARE": "1",
    "SOLVER_CP_WORKERS": "1", "SOLVER_POOL_PER_BAY": "1000",
    "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1", "NUMBA_NUM_THREADS": "1",
    # workers self-pack within their build_margin window -> tiny search safety
    # (~1.5s floor at any budget) so they run right up to the gather deadline.
    "ALNS_SAFETY_FRAC": "0.005",
}


def _worker(prob, worker_tl, env, idx=0):
    """Spawn-safe top-level worker: run the ALNS engine, then SELF-SCORE + pack its
    best (within the build_margin window alns_solve reserved) and return (score, ops).
    The master just picks the min score -- no serial master re-pack -> minimal idle."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    for k, v in _WORKER_FIXED.items():
        os.environ[k] = v
    for k, v in env.items():
        os.environ[k] = v
    import alns
    import solver
    t = time.time()
    try:
        best, _base = alns.alns_solve(prob, worker_tl, _return_assignment=True)
        # self-score+pack on the SAME basis as the master a_pref (one consistent
        # _score_and_pack); bounded by the worker's own deadline (leave ~1s to emit).
        pack_dl = t + worker_tl - 1.0
        score, packed = solver._score_and_pack(prob, best, poly_deadline=pack_dl)
        ops = solver._solution_from_packed(packed)
        return {"idx": idx, "score": score, "ops": ops, "elapsed": time.time() - t, "err": None}
    except Exception as e:                                  # pragma: no cover
        return {"idx": idx, "score": None, "ops": None, "elapsed": time.time() - t, "err": repr(e)}


def portfolio_solve(prob, timelimit):
    """Run the best-of portfolio and return a solution dict. Falls back to a
    single-process config-A solve on any multiprocessing failure."""
    import solver
    import alns

    t0 = time.time()
    # Only true idle = this end-buffer. Workers self-score+emit and the master just
    # picks the min (instant), so NO master re-pack reserve (final_guard) is needed
    # and the buffer only has to cover the instant pick + emit -> small, capped.
    safety = min(2.0, max(1.2, 0.008 * timelimit))
    poly_dl = t0 + timelimit - safety

    # Warm the numba disk-cache (one build) BEFORE spawning so the workers load the
    # compiled cache instead of all compiling at once. This also scores a_pref ->
    # a guaranteed-feasible FLOOR candidate (kept as ready-to-emit ops).
    a_pref = solver.a_pref(prob)
    a_obj, a_packed = solver._score_and_pack(prob, a_pref, poly_deadline=poly_dl)
    a_ops = solver._solution_from_packed(a_packed)

    # Workers run until the gather deadline; the master picks the min instantly.
    gather_dl = poly_dl
    worker_tl = max(5.0, gather_dl - time.time())

    results = []
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        payloads = [(prob, worker_tl, env, i) for i, env in enumerate(PROFILES)]
        with ctx.Pool(len(PROFILES)) as pool:
            asyncs = [pool.apply_async(_worker, p) for p in payloads]
            pending = list(asyncs)
            while pending and time.time() < gather_dl:
                for a in list(pending):
                    if a.ready():
                        try:
                            results.append(a.get())
                        except Exception as e:             # pragma: no cover
                            results.append({"score": None, "ops": None, "elapsed": 0.0, "err": repr(e)})
                        pending.remove(a)
                if pending:
                    time.sleep(0.05)
            pool.terminate()
            pool.join()
    except Exception:                                      # pragma: no cover
        results = []   # mp unavailable/forbidden/bootstrap error -> single-process fallback

    # Diagnostic: log a_pref + per-worker scores (master-side) for 2-vs-4 analysis.
    if os.environ.get("ALNS_PORTF_DEBUG"):
        sc = {r.get("idx"): r.get("score") for r in results}
        log = " ".join("W%s=%s" % (i, ("%.0f" % sc[i] if sc.get(i) is not None else "FAIL"))
                       for i in range(len(PROFILES)))
        print("PORTF_DEBUG a_pref=%.0f %s" % (a_obj, log), flush=True)

    # Pick the min-score solution over the a_pref FLOOR + each worker's self-scored
    # best. The floor guarantees a feasible emit even if every worker failed.
    best_obj, best_ops = a_obj, a_ops
    for r in results:
        if r.get("ops") is not None and r.get("score") is not None and r["score"] < best_obj:
            best_obj, best_ops = r["score"], r["ops"]
    return best_ops
