"""HELM parallel portfolio -- one ANCHOR per worker (env-gated by HELM_PORTFOLIO).

Identical harness to flux/prism portfolios (master builds anchors once -- MIP anchors use
the single-use WLS Gurobi license serially -- workers run pure NORECOMB LAHC, master
union-recombines + best-of + guarded idle-reclaim). The HELM difference is upstream: the
anchor spectrum handed to the workers is ROUTED per instance (helm_engine.classify).

Mirrors prism/portfolio.py robustness: spawn-safety probe + single-process fallback
(helm_solve) so enabling the gate can never do worse than serial HELM.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

LAST: dict = {}

_WORKER_FIXED = {
    "SOLVER_CP_WORKERS": "1",
    "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1", "NUMBA_NUM_THREADS": "1",
}


def _worker(prob, anchor, worker_tl, L, seed):
    """Spawn-safe top-level worker: refine ONE anchor with NORECOMB LAHC+ILS, return its
    assignment + column pool for the master union-recombine. `seed` = restart diversity."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    for k, v in _WORKER_FIXED.items():
        os.environ[k] = v
    import helm_engine as H
    t = time.time()
    try:
        best, pool, tot = H.refine_anchor(prob, anchor, worker_tl, L=L, seed=seed)
        return {"best": best, "pool": pool, "tot": tot, "elapsed": time.time() - t, "err": None}
    except Exception as e:                                  # pragma: no cover
        return {"best": None, "pool": {}, "tot": None, "elapsed": time.time() - t, "err": repr(e)}


def _probe():
    return 1


def portfolio_solve(prob, timelimit):
    """Run the HELM routed-anchor portfolio; fall back to serial helm_solve on mp failure."""
    import helm_engine as H
    K = H.K
    t0 = time.time()
    LAST.clear()
    LAST["mode"] = "portfolio"

    if os.environ.get("HELM_PORTFOLIO_PROBE", "1") not in ("0", "", None):
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
            return H.helm_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    L = int(os.environ.get("HELM_LAHC_L", os.environ.get("SOLVER_LAHC_L", "1")))
    safety = min(5.0, max(3.0, 0.025 * timelimit))
    poly_dl = t0 + timelimit - safety

    # MASTER routes + builds all anchors ONCE (MIP anchors -> serial single license use).
    regime, feats = H.classify(prob)
    H._route_env(regime)          # e.g. irreducible -> MO off (workers inherit via env)
    LAST["regime"] = regime
    LAST["feats"] = feats
    anchors, seed_base = H._anchors(prob, regime, want_mip=not H._env_flag("HELM_NO_MIP"))

    a_pref = K.a_pref(prob)
    tb = time.time()
    K._score_and_pack(prob, a_pref, poly_deadline=poly_dl)
    build_cost = time.time() - tb

    final_guard = min(0.40 * timelimit, max(6.0, build_cost * (len(anchors) + 1)))
    gather_dl = t0 + timelimit - safety - final_guard
    collect_margin = max(3.0, 0.05 * timelimit)
    worker_tl = max(5.0, gather_dl - time.time() - collect_margin)

    n = min(len(anchors), int(os.environ.get("HELM_PORTFOLIO_WORKERS", str(len(anchors)))))
    results = []
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        payloads = [(prob, anchors[i][1], worker_tl, L, seed_base + 1000 * i) for i in range(n)]
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

    if os.environ.get("HELM_PORTF_DEBUG"):
        sys.stderr.write("[HELM-PORTF] regime=%s anchors=%d workers=%d worker_tl=%.1f "
                         "build_cost=%.1f\n" % (regime, len(anchors), len(results),
                                                worker_tl, build_cost))
        for i, r in enumerate(results):
            sys.stderr.write("[HELM-PORTF] w%d(%s) elapsed=%.1f pool=%d tot=%s err=%s\n" % (
                i, anchors[i][0] if i < len(anchors) else "?", r.get("elapsed", -1),
                len(r.get("pool") or {}), r.get("tot"), r.get("err")))

    LAST["n_workers"] = len(results)
    LAST["worker_tot"] = [r.get("tot") for r in results]
    LAST["anchor_names"] = [anchors[i][0] for i in range(min(n, len(anchors)))]

    if not any(r.get("best") is not None for r in results):
        LAST["mode"] = "serial_fallback_no_worker"
        return H.helm_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    cands = [a_pref] + [r["best"] for r in results if r.get("best") is not None]

    # MASTER union-recombine over every worker's pool (one license use, no contention).
    if os.environ.get("HELM_PORTF_UNION_RECOMB", "1") not in ("0", "", None) and H._HAS_GUROBI:
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
                                      os.environ.get("HELM_PORTF_UNION_NOREL", "15"))
                rec = K._recombine(prob, a_pref, rec_dl)
                if rec is not None:
                    cands.append(rec)
                    LAST["union_recomb"] = True
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
        return H.helm_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    # IDLE RECLAIM: guarded ILS from the best assignment on the master's leftover time
    # (monotonic: adopt only if the TRUE best-of score improves; bounded by poly_dl).
    if os.environ.get("HELM_PORTF_IDLE_RECLAIM", "1") not in ("0", "", None):
        emit_margin = max(1.5, build_cost * 1.3)
        idle_dl = poly_dl - emit_margin
        if best_asg is not None and time.time() < idle_dl - 2.0:
            try:
                K._EVAL_LIMIT = None
                A2, _, _ = H.refine_anchor(prob, best_asg, idle_dl - time.time(),
                                           L=L, seed=seed_base)
                obj2, packed2 = K._score_and_pack(prob, A2, poly_deadline=poly_dl)
                if obj2 < best_obj - 1e-9:
                    best_obj, best_packed = obj2, packed2
                    LAST["idle_reclaim_improved"] = True
            except Exception as _e:                        # pragma: no cover
                LAST["idle_reclaim_err"] = repr(_e)
    LAST["final_obj"] = round(best_obj)
    LAST["n_cands"] = len(cands)
    return K._solution_from_packed(best_packed)
