"""Parallel multi-start orchestration for Athena SA."""
from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import random
import time

from utils import Bay

from .events import _close_event_log, _emit, _init_event_log
from .features import precompute_features
from .sa import SA_PROFILES, sa_loop

def _sa_worker(payload: dict) -> dict:
    """Top-level (picklable) SA worker entry point for ProcessPoolExecutor.

    Receives only plain, picklable data. Shapely-backed `Features` are NOT
    sent across the process boundary (Polygon pickling is fragile); each worker
    rebuilds bays + features from `prob_info`. The feature precompute is cheap
    relative to the SA budget (<~0.1s for the benchmark sizes), so recomputing
    per worker is acceptable and keeps workers fully independent (no shared
    state). The input assignment is deepcopied before the worker mutates it.
    """
    seed = payload["seed"]
    profile = payload["profile"]
    prof_name = profile.get("name") if isinstance(profile, dict) else None
    try:
        random.seed(seed)

        # Per-worker event log to avoid interleaved writes into one file.
        # When forking, the child inherits the parent's open log handle; we
        # rebind it here (to a per-worker file or to None) so workers never
        # write into the shared parent descriptor.
        ev_path = payload.get("event_log")
        if ev_path:
            os.environ["OGC2026_EVENT_LOG"] = ev_path
        else:
            os.environ.pop("OGC2026_EVENT_LOG", None)
        _init_event_log(time.time())

        prob_info = payload["prob_info"]
        w1, w2, w3 = payload["weights"]
        deadline = payload["deadline"]

        bays = [Bay.from_dict(d, i) for i, d in enumerate(prob_info["bays"])]
        F = precompute_features(prob_info, bays)
        initial = {int(k): dict(v) for k, v in payload["initial_assignments"].items()}

        _emit("sa.worker.start", seed=seed, profile=prof_name)
        best_assign, best_sol, best_obj, iters, improvements = sa_loop(
            prob_info, F, bays, w1, w2, w3, initial, deadline, profile=profile,
        )
        feasible = best_obj != float("inf")
        _emit("sa.worker.done", seed=seed, profile=prof_name,
              feasible=feasible, objective=best_obj, iterations=iters)
        return {
            "ok": True,
            "best_assignments": best_assign if feasible else None,
            "best_objective": best_obj,
            "feasible": feasible,
            "iterations": iters,
            "improvements": improvements,
            "seed": seed,
            "profile": prof_name,
        }
    except Exception as exc:  # never let a worker crash take down the solver
        return {
            "ok": False,
            "error": repr(exc),
            "best_assignments": None,
            "best_objective": float("inf"),
            "feasible": False,
            "iterations": 0,
            "improvements": 0,
            "seed": seed,
            "profile": prof_name,
        }
    finally:
        _close_event_log()


def parallel_sa_multi_start(prob_info: dict, initial_assignments: dict,
                            w1: float, w2: float, w3: float,
                            worker_deadline: float, gather_deadline: float,
                            max_workers: int, base_seed: int,
                            event_log_base: str | None = None,
                            profiles_override: list[dict] | None = None
                            ) -> tuple[dict | None, float, list[dict]]:
    """Run up to `max_workers` (<=4) independent SA workers in parallel.

    Each worker stops its SA loop at `worker_deadline`; the main process stops
    waiting for stragglers at `gather_deadline` (which must be <= the overall
    hard deadline) so the solve never overruns its time budget.

    Returns (best_assignments_or_None, best_objective, worker_results).
    The caller is responsible for falling back to the initial / single-start
    solution when this returns None. Process-based (not threads) to sidestep
    the GIL; failures and timeouts are contained so the overall solve survives.
    """
    n_workers = max(1, min(4, max_workers))
    if profiles_override is not None:
        profiles = [profiles_override[i % len(profiles_override)]
                    for i in range(n_workers)]
    else:
        profiles = [SA_PROFILES[i % len(SA_PROFILES)] for i in range(n_workers)]

    payloads = []
    for idx, prof in enumerate(profiles):
        ev = f"{event_log_base}.worker{idx}" if event_log_base else None
        payloads.append({
            "prob_info": prob_info,
            "initial_assignments": initial_assignments,
            "weights": (w1, w2, w3),
            "profile": prof,
            "seed": base_seed + idx * 7919 + 1,
            "deadline": worker_deadline,
            "event_log": ev,
        })

    # Prefer fork on POSIX (cheap, no module re-import); spawn elsewhere.
    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:
        ctx = multiprocessing.get_context("spawn")

    results: list[dict] = []
    best_assign: dict | None = None
    best_obj = float("inf")

    # Wait until `gather_deadline` for workers to wrap up and return their
    # result (they stop their own SA loop earlier, at `worker_deadline`), but
    # never block the caller past the overall hard deadline.
    gather_timeout = max(0.0, gather_deadline - time.time())

    executor = None
    try:
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=n_workers, mp_context=ctx)
        futures = {executor.submit(_sa_worker, p): p["profile"]["name"]
                   for p in payloads}
        for fut in concurrent.futures.as_completed(futures, timeout=gather_timeout):
            try:
                res = fut.result()
            except Exception as exc:
                results.append({"ok": False, "error": repr(exc),
                                "feasible": False, "best_objective": float("inf")})
                continue
            results.append(res)
            if res.get("feasible") and res.get("best_assignments") is not None:
                if res["best_objective"] < best_obj:
                    best_obj = res["best_objective"]
                    best_assign = res["best_assignments"]
    except concurrent.futures.TimeoutError:
        _emit("sa.parallel.gather_timeout",
              collected=len(results), n_workers=n_workers)
    except Exception as exc:
        # Pool spawn / submission failure -- signal fallback to single-start.
        _emit("sa.parallel.error", error=repr(exc))
        return None, float("inf"), results
    finally:
        if executor is not None:
            # Don't wait on stragglers; cancel anything not yet started.
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # Python < 3.9 has no cancel_futures
                executor.shutdown(wait=False)

    return best_assign, best_obj, results
