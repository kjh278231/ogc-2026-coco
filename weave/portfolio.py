"""WEAVE parallel portfolio -- an ISLAND MODEL (env-gated by WEAVE_PORTFOLIO).

Each worker runs a FULL WEAVE population (seed-diversified island) from the SAME precomputed
anchors, NORECOMB; the master then unions all island pools and runs ONE column-recombine,
best-of true-scores, and idle-reclaims. Like PRISM's portfolio, the MIP anchors are computed
ONCE in the master (single-use WLS Gurobi license -> never concurrently in workers).

Mirrors prism/portfolio.py's hard-won robustness: spawn-safety probe + single-process
fallback (weave_solve) so enabling the gate can never do worse than serial WEAVE.
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
    "WEAVE_PORTFOLIO": "0",   # workers never re-enter the portfolio
}


def _worker(prob, kind, anchor_arg, worker_tl, L, seed):
    """Spawn-safe worker. kind="weave": run a full WEAVE population from a precomputed anchor
    list (refine_population). kind="prism": refine ONE anchor PRISM-style (refine_anchor,
    single-basin LAHC+ILS). Both NORECOMB (master does the single union-recombine). The HYBRID
    portfolio mixes the two so best-of captures whichever paradigm wins per instance."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    for k, v in _WORKER_FIXED.items():
        os.environ[k] = v
    os.environ["SOLVER_NORECOMB"] = "1"   # master does the single union-recombine
    t = time.time()
    try:
        if kind == "prism":
            _pd = os.path.join(os.path.dirname(_HERE), "prism")   # dev layout; flat zip = sibling
            if _pd not in sys.path:
                sys.path.insert(0, _pd)
            import prism_engine as P
            best, pool, tot = P.refine_anchor(prob, anchor_arg, worker_tl, L=L, seed=seed)
        else:
            import weave_engine as WE
            best, pool, tot = WE.refine_population(prob, anchor_arg, worker_tl, L=L, seed=seed)
        return {"best": best, "pool": pool, "tot": tot, "elapsed": time.time() - t, "err": None}
    except Exception as e:                                  # pragma: no cover
        return {"best": None, "pool": {}, "tot": None, "elapsed": time.time() - t, "err": repr(e)}


def _pick_prism_anchors(anchors, k=2):
    """Choose k anchor assignments for PRISM-style single-basin workers, preferring the
    historically strongest (mip*, then capped) — the wall LAST diag showed these win per
    instance. Returns k assignment dicts."""
    order = sorted(anchors, key=lambda a: (0 if "mip" in a[0] else (1 if a[0] == "capped" else 2)))
    picks = [a[1] for a in order[:k]]
    return picks or [anchors[0][1]]


def _probe():
    return 1


def _island_subset(anchors, i, n, k):
    """Island i gets a rotating WINDOW of `k` anchors (k<=0 -> the full set). k=1 makes each island
    a single-basin worker (PRISM-like, no population overhead) -- used at SHORT budgets where the
    pool-build overhead starves each island (T13@60 +59% regression); k=0 (full pop per island) is
    WEAVE's long-budget strength. k>=2 window was tested at T=180 (regressed T20) -> not a blanket win."""
    m = len(anchors)
    if k <= 0 or m <= k:
        return anchors
    return [anchors[(i + j) % m] for j in range(k)]


def portfolio_solve(prob, timelimit):
    """Run the WEAVE island portfolio; fall back to serial weave_solve on any mp failure."""
    import weave_engine as WE
    K = WE.K
    t0 = time.time()
    LAST.clear()
    LAST["mode"] = "portfolio"

    if os.environ.get("WEAVE_PORTFOLIO_PROBE", "1") not in ("0", "", None):
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
            return WE.weave_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    L = int(os.environ.get("WEAVE_LAHC_L", os.environ.get("SOLVER_LAHC_L", "1")))
    safety = min(5.0, max(3.0, 0.025 * timelimit))
    poly_dl = t0 + timelimit - safety

    # MASTER computes anchors ONCE (MIP serially -> single license use).
    mip_tl = float(os.environ.get("WEAVE_MIP_TL", "4.0"))
    anchors = WE._anchors(prob, mip_tl, want_mip=not WE._flag("WEAVE_NO_MIP"))

    a_pref = K.a_pref(prob)
    tb = time.time()
    K._score_and_pack(prob, a_pref, poly_deadline=poly_dl)
    build_cost = time.time() - tb

    # Deep-anchor injection (WEAVE_DEEP_ANCHOR, default OFF -- TRIED, REJECTED): the idea was that
    # islands refine each anchor only shallowly (pool_frac), missing the deep-Z3 basin PRISM's
    # dedicated single-anchor worker reaches (T18/T12/T17 losses); so the master deep-refines mip16
    # once and injects it as an extra anchor. RESULT: T18 +5%, T12 -2.6%, but T20 +39% (149515 vs
    # 107328) -- the 6th anchor + ~20s worker-time cost gutted the recombination that WEAVE wins with.
    # Same instance-split wall as POOL_FRAC/island_k/MAXPOP -> WEAVE's losses are inherent to the
    # recombination-vs-depth trade. Kept as a resident off-by-default knob; default = original anchors.
    if os.environ.get("WEAVE_DEEP_ANCHOR", "0") not in ("0", "", None) and anchors:
        try:
            strong = sorted(anchors, key=lambda a: (0 if a[0] == "mip16" else 1))[0][1]
            da_s = min(float(os.environ.get("WEAVE_DEEP_ANCHOR_S", "20")), 0.15 * timelimit)
            K._EVALS = 0
            K._EVAL_LIMIT = None
            _c0 = {}
            deep, _ = K._climb_lahc(prob, strong, _c0, time.time() + da_s, L)
            if K._env_flag("SOLVER_SWAP"):
                deep, _ = K._z3_refine(prob, deep, _c0, time.time() + max(1.0, da_s * 0.3))
            anchors = anchors + [("deep_mip16", deep)]
        except Exception:
            pass

    final_guard = min(0.40 * timelimit, max(6.0, build_cost * 3))
    gather_dl = t0 + timelimit - safety - final_guard
    collect_margin = max(3.0, 0.05 * timelimit)
    worker_tl = max(5.0, gather_dl - time.time() - collect_margin)

    n = int(os.environ.get("WEAVE_PORTFOLIO_WORKERS", "4"))
    results = []
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        if os.environ.get("WEAVE_HYBRID") not in (None, "", "0"):
            # HYBRID: 2 PRISM single-basin workers (strongest anchors) + 2 WEAVE island workers.
            # best-of + union-recombine captures whichever paradigm wins per instance (WEAVE on
            # T13/T20 via PR+population, PRISM on T14/T18 via its winning anchor). Env-tunable split.
            npr = int(os.environ.get("WEAVE_HYBRID_PRISM", "2"))
            pa = _pick_prism_anchors(anchors, npr)
            payloads = [(prob, "prism", pa[i % len(pa)], worker_tl, L, 20260702 + 1000 * i)
                        for i in range(npr)]
            nw = max(1, n - npr)
            payloads += [(prob, "weave", anchors, worker_tl, L, 20270702 + 1000 * i)
                         for i in range(nw)]
            n = len(payloads)
        elif timelimit < float(os.environ.get("WEAVE_FULLPOP_MIN_T", "150")):
            # SHORT-budget robustness (budget-gated ensemble): the full-population pool-build overhead
            # starves each island on a tight budget -> under-convergence on hard instances (T13@60
            # 147698 vs PRISM 92660). Below WEAVE_FULLPOP_MIN_T, run PRISM-style single-basin workers
            # (one distinct anchor each, refine_anchor with ILS kicks) = PRISM's short-budget strength,
            # no population overhead. Above it, WEAVE population islands = WEAVE's long-budget edge.
            # pick the strongest n anchors (mip16 + heuristic trio; drop the weak mip1) so the
            # short-budget workers match PRISM's proven set -- WEAVE_LAMBDAS="1,16" yields 5 anchors
            # and 4 workers would otherwise take the first 4 and DROP mip16 (PRISM's per-instance winner).
            strong = sorted(anchors, key=lambda a: (0 if a[0] == "mip16"
                            else (1 if a[0] in ("pref", "capped", "balanced") else 2)))
            sel = strong[:n]
            payloads = [(prob, "prism", sel[i % len(sel)][1], worker_tl, L, 20260702 + 1000 * i)
                        for i in range(n)]
        else:
            island_k = int(os.environ.get("WEAVE_ISLAND_K", "0"))
            payloads = [(prob, "weave", _island_subset(anchors, i, n, island_k), worker_tl, L,
                         20260702 + 1000 * i) for i in range(n)]
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

    if os.environ.get("WEAVE_PORTF_DEBUG"):
        sys.stderr.write("[WEAVE-PORTF] anchors=%d workers=%d worker_tl=%.1f build_cost=%.1f\n" % (
            len(anchors), len(results), worker_tl, build_cost))
        for i, r in enumerate(results):
            sys.stderr.write("[WEAVE-PORTF] w%d elapsed=%.1f pool=%d tot=%s err=%s\n" % (
                i, r.get("elapsed", -1), len(r.get("pool") or {}), r.get("tot"), r.get("err")))

    LAST["n_workers"] = len(results)
    LAST["worker_tot"] = [r.get("tot") for r in results]
    LAST["worker_err"] = [r.get("err") for r in results]

    if not any(r.get("best") is not None for r in results):
        LAST["mode"] = "serial_fallback_no_worker"
        return WE.weave_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    cands = [a_pref] + [r["best"] for r in results if r.get("best") is not None]

    # MASTER union-recombine over every island's pool (one license use, no contention).
    if os.environ.get("WEAVE_UNION_RECOMB", "1") not in ("0", "", None) and K._HAS_GUROBI:
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
                                      os.environ.get("WEAVE_UNION_NOREL", "15"))
                rec = K._recombine(prob, cands[1] if len(cands) > 1 else a_pref, rec_dl)
                if rec is not None:
                    cands.append(rec)
                    LAST["union_recomb"] = True
                    LAST["union_pool"] = len(union)
        except Exception as _e:                            # pragma: no cover
            LAST["union_err"] = repr(_e)

    # Best-of on the ACHIEVABLE emit quality: score each candidate with the SAME poly_deadline
    # the final build uses, so the emitted solution is the truly-best one that can be packed
    # within the time budget. (At very short budgets a DENSE low-Z3 worker solution may only
    # AABB-pack -> Z1>0 -> correctly loses to the AABB-robust a_pref; at T>=90 there is build
    # time to mask-pack it and it wins big -- e.g. T1 4539 vs a_pref 15611, -71%.) Pareto-safe:
    # a_pref is always a candidate, so the emit is never worse than the baseline floor.
    K._EVAL_LIMIT = None
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
        return WE.weave_solve(prob, max(1.0, timelimit - (time.time() - t0)))

    # IDLE RECLAIM: spend leftover wall on a guarded population from the best (monotonic).
    if os.environ.get("WEAVE_IDLE_RECLAIM", "1") not in ("0", "", None):
        emit_margin = max(1.5, build_cost * 1.3)
        idle_dl = poly_dl - emit_margin
        if best_asg is not None and time.time() < idle_dl - 2.0:
            try:
                K._EVAL_LIMIT = None
                A2, _, _ = WE.refine_population(prob, [("best", best_asg)],
                                                idle_dl - time.time(), L=L, seed=20260702)
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
