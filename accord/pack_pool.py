"""Bay-parallel packing pool for ACCORD (env ACCORD_PAR, default OFF until validated).

ACCORD's oracle packs each bay of an iterate independently, and on pack-heavy
instances the eval IS the iteration cost: the v3 log shows T20 budget-bound (refine
never fires, still improving at cutoff) and T38 eval-starved (2 iterations @60).
A pool of spawn-safe workers packs the cache-miss bays of each iterate in parallel.

Why this beats a trajectory-diverse portfolio for the T20 class:
  * pack results are deterministic per (bay, ids) -> the price-loop trajectory stays
    EXACTLY the single-core one, just faster = a pure-throughput lever, no quality
    risk (the accumulating congestion map is T20's asset; splitting cores across
    diverse trajectories would starve it);
  * packing never touches Gurobi -> no single-use-license contention (the price MIP
    stays in the master, one env, serial) [[gurobi-single-use-license]];
  * spawn-probe + serial fallback ([[portfolio-spawn-guard]]) so enabling the gate
    can never do worse than the serial oracle.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE = os.path.join(os.path.dirname(_HERE), "bridge")

_WORKER_FIXED = {
    "SOLVER_CP_WORKERS": "1",
    "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1", "NUMBA_NUM_THREADS": "1",
}

# kernel gates the worker must replicate BEFORE importing the solver module
_ENV_KEYS = ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_NUMBA", "SOLVER_MASK_PREPARE",
             "SOLVER_MULTIORDER", "SOLVER_MO_ORDERS", "SOLVER_MASK_R", "SOLVER_NOPOLY")

_G: dict = {}      # worker globals


def _init(prob, env):
    for k, v in _WORKER_FIXED.items():
        os.environ[k] = v
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for p in (_HERE, _BRIDGE):
        if p not in sys.path:
            sys.path.append(p)
    import solver as K                       # noqa
    from packing import solve_bay, solve_bay_best, extract_tardiness  # noqa
    _G.update(prob=prob, K=K, solve_bay=solve_bay, solve_bay_best=solve_bay_best,
              extract_tardiness=extract_tardiness)


def _pack(job):
    """One bay pack -- the exact body of accord_engine._Oracle.pack_bay, stateless."""
    j, ids = job
    K = _G["K"]
    prob = _G["prob"]
    blocks = prob["blocks"]
    lids = list(ids)
    if K._MULTIORDER:
        placed, _, _ = _G["solve_bay_best"](prob, j, lids,
                                            mask=K._MASK_SEARCH, mask_R=K._MASK_R_SEARCH)
    else:
        placed = _G["solve_bay"](prob, j, lids,
                                 mask=K._MASK_SEARCH, mask_R=K._MASK_R_SEARCH)
    T, exited = _G["extract_tardiness"](prob, j, placed)
    tau = {i: max(0.0, exited[i] - blocks[i]["due_date"]) for i in lids}
    blockers = {}
    ent = {p["id"]: p["entry"] for p in placed}
    ext = {p["id"]: p["exit"] for p in placed}
    for i, t in tau.items():
        if t > 0:
            rel = blocks[i]["release_time"]
            bl = tuple(k for k in lids if k != i and ent[k] < ent[i] and ext[k] > rel)
            if bl:
                blockers[i] = bl
    return (j, ids, T, tau, blockers)


def _probe():
    return 1


def start(prob, timelimit, n=None):
    """Spawn the pack pool (probe-guarded). Returns a Pool or None (= use serial)."""
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        with ctx.Pool(1) as pp:
            if pp.apply_async(_probe).get(timeout=min(20.0, max(5.0, 0.1 * timelimit))) != 1:
                raise RuntimeError("probe mismatch")
        env = {k: os.environ.get(k) for k in _ENV_KEYS}
        n = n or int(os.environ.get("ACCORD_PAR_WORKERS", "4"))
        return ctx.Pool(n, initializer=_init, initargs=(prob, env))
    except Exception:
        return None
