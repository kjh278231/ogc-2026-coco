"""PRISM parallel portfolio -- one ANCHOR per worker (env-gated by PRISM_PORTFOLIO).

The PRISM thesis is anchor diversity: different preference/balance ideals fall into
different Z1=0 basins, and best-of captures whichever wins per instance. Serially the
anchors must share one eval/time budget (each gets ~1/N), which starves the anchor that
would otherwise win. On the 4-core eval machine the natural fix is one anchor per core,
each a FULL-budget worker:

    MASTER computes all anchors ONCE (the MIP anchors use the single-use WLS Gurobi
    license, so they MUST be computed serially in the master -- never concurrently in
    workers) -> spawn N workers, each runs pure NORECOMB LAHC from its anchor (no Gurobi,
    no contention) -> MASTER gathers, runs ONE union-recombine over all pools, and emits
    the best-of true-scored candidate.

Mirrors bridge/portfolio.py's robustness: a spawn-safety probe + single-process fallback
(prism_solve) so enabling the gate can never do worse than serial PRISM.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

LAST: dict = {}   # diagnostics from the last portfolio_solve (spawned?, worker objs, ...)

_WORKER_FIXED = {
    "SOLVER_CP_WORKERS": "1",
    "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1", "NUMBA_NUM_THREADS": "1",
}


def _worker(prob, anchor, worker_tl, L, seed):
    """Spawn-safe top-level worker: refine ONE anchor with NORECOMB LAHC+ILS, return its
    assignment + column pool (for the master union-recombine). `seed` gives each worker a
    distinct ILS/mover-shuffle trajectory (restart diversity)."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    for k, v in _WORKER_FIXED.items():
        os.environ[k] = v
    import prism_engine as P
    t = time.time()
    try:
        best, pool, tot = P.refine_anchor(prob, anchor, worker_tl, L=L, seed=seed)
        return {"best": best, "pool": pool, "tot": tot, "elapsed": time.time() - t, "err": None}
    except Exception as e:                                  # pragma: no cover
        return {"best": None, "pool": {}, "tot": None, "elapsed": time.time() - t, "err": repr(e)}


def _probe():
    return 1


def portfolio_solve(prob, timelimit):
    """Run the PRISM anchor portfolio; fall back to serial prism_solve on any mp failure."""
    import prism_engine as P
    K = P.K
    t0 = time.time()
    LAST.clear()
    LAST["mode"] = "portfolio"

    # spawn-safety probe (same rationale as bridge/portfolio.py)
    if os.environ.get("PRISM_PORTFOLIO_PROBE", "1") not in ("0", "", None):
        try:
            import multiprocessing as _mp
            _ctx = _mp.get_context("spawn")
            with _ctx.Pool(1) as _pp:
                _r = _pp.apply_async(_probe)
                if _r.get(timeout=min(25.0, max(5.0, 0.15 * timelimit))) != 1:
                    raise RuntimeError("probe mismatch")
        except Exception as _e:
            LAST["mode"] = "serial_fallback_probe"
            LAST["probe_err"] = repr(_e)
            return P.prism_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    L = int(os.environ.get("PRISM_LAHC_L", os.environ.get("SOLVER_LAHC_L", "1")))
    # safety = the buffer AFTER poly_dl for the final emit + IPC/terminate overhead. The old
    # 0.04*T ballooned to 7.2s @180 (=most of the wall margin). The final build degrades to AABB
    # at poly_dl (bounded), so emit only needs ~2-3s; clamp to 3-5s -> wall margin ~3-5s
    # (checklist §1) while staying overrun-safe. The idle-reclaim ILS fills up to poly_dl.
    safety = min(5.0, max(3.0, 0.025 * timelimit))
    poly_dl = t0 + timelimit - safety

    # MASTER computes all anchors ONCE (MIP serially -> single license use).
    mip_tl = float(os.environ.get("PRISM_MIP_TL", "4.0"))
    anchors = P._anchors(prob, mip_tl, want_mip=not P._env_flag("PRISM_NO_MIP"))

    # warm numba disk-cache + measure build cost before spawning
    a_pref = K.a_pref(prob)
    tb = time.time()
    K._score_and_pack(prob, a_pref, poly_deadline=poly_dl)
    build_cost = time.time() - tb

    final_guard = min(0.40 * timelimit, max(6.0, build_cost * (len(anchors) + 1)))
    gather_dl = t0 + timelimit - safety - final_guard
    # Worker COLLECTION margin: the worker must FINISH and return BEFORE the master stops
    # gathering at gather_dl, else pool.terminate() kills it mid-run and its result is lost
    # -> the master sees no results and falls back to a time-starved serial solve (the cause
    # of the catastrophic ILS-loop run: workers that fill the budget were all killed before
    # returning). Unlike BRIDGE's framework_solve (which reserves its own internal safety),
    # PRISM's _refine ILS loop runs right up to its deadline, so we shave a collection margin
    # off the worker budget here. The master gather loop still polls until gather_dl.
    collect_margin = max(3.0, 0.05 * timelimit)
    worker_tl = max(5.0, gather_dl - time.time() - collect_margin)

    n = min(len(anchors), int(os.environ.get("PRISM_PORTFOLIO_WORKERS", str(len(anchors)))))
    results = []
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        payloads = [(prob, anchors[i][1], worker_tl, L, 20260629 + 1000 * i) for i in range(n)]
        with ctx.Pool(n) as pool:
            asyncs = [pool.apply_async(_worker, p) for p in payloads]
            pending = list(asyncs)
            while pending and time.time() < gather_dl:
                for a in list(pending):
                    if a.ready():
                        try:
                            results.append(a.get())
                        except Exception as e:             # pragma: no cover
                            results.append({"best": None, "pool": {}, "err": repr(e)})
                        pending.remove(a)
                if pending:
                    time.sleep(0.05)
            pool.terminate()
            pool.join()
    except Exception:                                      # pragma: no cover
        results = []

    if os.environ.get("PRISM_PORTF_DEBUG"):
        sys.stderr.write("[PRISM-PORTF] anchors=%d workers=%d worker_tl=%.1f build_cost=%.1f\n" % (
            len(anchors), len(results), worker_tl, build_cost))
        for i, r in enumerate(results):
            sys.stderr.write("[PRISM-PORTF] w%d(%s) elapsed=%.1f pool=%d tot=%s err=%s\n" % (
                i, anchors[i][0] if i < len(anchors) else "?", r.get("elapsed", -1),
                len(r.get("pool") or {}), r.get("tot"), r.get("err")))

    LAST["n_workers"] = len(results)
    LAST["worker_tot"] = [r.get("tot") for r in results]
    LAST["worker_pool_sizes"] = [len(r.get("pool") or {}) for r in results]
    LAST["worker_err"] = [r.get("err") for r in results]
    LAST["anchor_names"] = [anchors[i][0] for i in range(min(n, len(anchors)))]

    if not any(r.get("best") is not None for r in results):
        LAST["mode"] = "serial_fallback_no_worker"
        return P.prism_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    cands = [a_pref] + [r["best"] for r in results if r.get("best") is not None]

    # MASTER union-recombine over every worker's pool (one license use, no contention).
    if os.environ.get("PRISM_PORTF_UNION_RECOMB", "1") not in ("0", "", None) and P._HAS_GUROBI:
        try:
            union = {}
            for r in results:
                if r.get("pool"):
                    union.update(r["pool"])
            rec_dl = poly_dl - max(2.0, build_cost * (len(cands) + 1))
            if union and rec_dl > time.time() + 3.0:
                K._POOL.clear()
                K._POOL.update(union)
                os.environ.setdefault("SOLVER_RECOMB_NOREL",
                                      os.environ.get("PRISM_PORTF_UNION_NOREL", "15"))
                rec = K._recombine(prob, a_pref, rec_dl)
                if rec is not None:
                    cands.append(rec)
                    LAST["union_recomb"] = True
                    LAST["union_pool"] = len(union)
        except Exception as _e:                            # pragma: no cover
            LAST["union_err"] = repr(_e)

    best_obj, best_packed, best_asg = float("inf"), None, None
    seen = set()
    for asg in cands:
        key = frozenset(asg.items())
        if key in seen:
            continue
        seen.add(key)
        try:
            obj, packed = K._score_and_pack(prob, asg, poly_deadline=poly_dl)
        except Exception:                                  # pragma: no cover
            continue
        if obj < best_obj:
            best_obj, best_packed, best_asg = obj, packed, asg

    if best_packed is None:                                # pragma: no cover
        LAST["mode"] = "serial_fallback_no_packed"
        return P.prism_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    # Checklist §1 — IDLE RECLAIM: the workers stop at gather_dl and the master finishes
    # scoring/recombine well before poly_dl, leaving ~12-20s idle (target ≤5s). Spend the
    # leftover on a guarded ILS from the best assignment (monotonic: adopt only if the TRUE
    # best-of score improves -> can never regress), then materialize that. Reserve a build
    # margin so the final score+emit still finishes before poly_dl (no overrun). 1-core
    # (workers are done), but the time was idle. Gate PRISM_PORTF_IDLE_RECLAIM (default on).
    if os.environ.get("PRISM_PORTF_IDLE_RECLAIM", "1") not in ("0", "", None):
        emit_margin = max(1.5, build_cost * 1.3)  # room for the final A2 score+emit before poly_dl
        idle_dl = poly_dl - emit_margin
        if best_asg is not None and time.time() < idle_dl - 2.0:
            try:
                K._EVAL_LIMIT = None
                import random as _rnd
                # (Tried ejection-chain in the guarded idle-reclaim: NO-OP -- the final best is already
                # an ejection local optimum, so polishing it finds nothing. Ejection's T13 win comes
                # from workers running it DURING refinement into different basins, which costs worker
                # budget and regressed T20 +1.4%. Instance-split, no free lunch -> not adopted.)
                A2, _, _ = P.refine_anchor(prob, best_asg, idle_dl - time.time(),
                                           L=L, seed=20260629)
                obj2, packed2 = K._score_and_pack(prob, A2, poly_deadline=poly_dl)
                LAST["idle_reclaim_obj"] = round(obj2)
                if obj2 < best_obj - 1e-9:
                    best_obj, best_packed = obj2, packed2
                    LAST["idle_reclaim_improved"] = True
            except Exception as _e:                        # pragma: no cover
                LAST["idle_reclaim_err"] = repr(_e)
    LAST["final_obj"] = round(best_obj)
    LAST["n_cands"] = len(cands)
    return K._solution_from_packed(best_packed)
