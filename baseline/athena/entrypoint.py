"""Public Athena solver entrypoint."""
from __future__ import annotations

import contextlib
import os
import random
import sys
import time

from utils import Bay, Block

from .events import _close_event_log, _emit, _init_event_log
from .features import precompute_features, smooth_time_windows
from .parallel import parallel_sa_multi_start
from .placement import place_initial
from .repair import build_safe_serial_assignments, repair_conflict_closure
from .sa import _DEFAULT_PROFILE, sa_loop
from .solution import evaluate_solution

# Public API
# -----------------------------------------------------------------------------

@contextlib.contextmanager
def _silence_stdout():
    saved = sys.stdout
    try:
        sys.stdout = open(os.devnull, "w")
        yield
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.stdout = saved


def _make_time_plan(t_start: float, timelimit: float, hard_deadline: float,
                    safety: float, n_blocks: int) -> tuple:
    t = max(0.0, timelimit)
    if t <= 45.0:
        tier = "short"
        init_duration = max(2.0, t * (0.30 if n_blocks >= 150 else 0.25))
        fallback_duration = max(3.0, t * 0.45)
        repair_duration = max(4.0, t * 0.55)
    elif t <= 180.0:
        tier = "medium"
        init_duration = max(2.0, t * (0.45 if n_blocks >= 150 else 0.35))
        fallback_duration = max(4.0, t * 0.55)
        repair_duration = max(5.0, t * 0.65)
    else:
        tier = "long"
        init_duration = min(60.0, max(20.0, t * 0.20))
        fallback_duration = min(75.0, max(30.0, t * 0.30))
        repair_duration = min(90.0, max(45.0, t * 0.35))

    gather_margin = min(0.5, max(0.05, t * 0.01))
    soft_end = hard_deadline - safety
    deadline = lambda d: min(soft_end, t_start + max(0.0, d))
    return (tier, deadline(init_duration), deadline(fallback_duration),
            deadline(repair_duration), deadline(max(4.0, t * 0.92)),
            hard_deadline - gather_margin)


def algorithm(prob_info: dict, timelimit: float = 60.0) -> dict:
    """Athena solver entry point. Returns a solution dict in the canonical
    {"operations": {time_str: [op,...]}} format."""
    t_start = time.time()
    _init_event_log(t_start)
    _emit("algo.start", timelimit=timelimit)

    hard_deadline = t_start + max(0.0, timelimit)
    safety = min(0.5, max(0.05, timelimit * 0.02))

    bays = [Bay.from_dict(d, i) for i, d in enumerate(prob_info["bays"])]
    blocks = prob_info["blocks"]
    n = len(blocks)
    w1 = float(prob_info.get("weights", {}).get("w1", 1.0))
    w2 = float(prob_info.get("weights", {}).get("w2", 1.0))
    w3 = float(prob_info.get("weights", {}).get("w3", 1.0))

    _emit("algo.context", n_blocks=n, n_bays=len(bays), w1=w1, w2=w2, w3=w3,
          algo="athena")

    # Phase 1
    F = precompute_features(prob_info, bays)
    _emit("athena.features.done", elapsed=round(time.time() - t_start, 3))
    (time_tier, init_deadline, fallback_deadline, repair_deadline,
     sa_deadline, gather_deadline) = _make_time_plan(
        t_start, timelimit, hard_deadline, safety, n,
    )
    _emit("athena.time_plan",
          tier=time_tier,
          init_deadline=round(init_deadline - t_start, 3),
          fallback_deadline=round(fallback_deadline - t_start, 3),
          repair_deadline=round(repair_deadline - t_start, 3),
          sa_deadline=round(sa_deadline - t_start, 3),
          gather_deadline=round(gather_deadline - t_start, 3))

    emergency_assign = None
    emergency_res = None
    emergency_sol = {"operations": {}}
    emergency_obj = float("inf")
    emergency_missing = 0
    if time.time() < hard_deadline - safety:
        emergency_assign, emergency_missing = build_safe_serial_assignments(
            prob_info, F, bays,
        )
        emergency_res, emergency_sol = evaluate_solution(prob_info, emergency_assign)
        emergency_obj = (
            float(emergency_res["objective"]) if emergency_res["feasible"] else float("inf")
        )
        _emit("athena.init.emergency",
              elapsed=round(time.time() - t_start, 3),
              feasible=bool(emergency_res["feasible"]),
              stage=str(emergency_res.get("stage")),
              objective=emergency_obj,
              missing=emergency_missing)

    # Phase 2
    target_entry, target_orient = smooth_time_windows(prob_info, F)
    _emit("athena.smoothing.done", elapsed=round(time.time() - t_start, 3))

    # Phase 4 (uses phase 3 ranker internally)
    assignments, n_forced, n_unplaced = place_initial(
        prob_info, F, bays,
        target_entry, target_orient,
        w1, w2, w3,
        init_deadline,
    )
    init_res, init_sol = evaluate_solution(prob_info, assignments)
    init_obj = float(init_res["objective"]) if init_res["feasible"] else float("inf")
    _emit("athena.init.done",
          elapsed=round(time.time() - t_start, 3),
          feasible=bool(init_res["feasible"]),
          stage=str(init_res.get("stage")),
          objective=init_obj,
          n_forced=n_forced,
          n_unplaced=n_unplaced)

    init_tardiness = float(init_res.get("obj1") or 0.0) if init_res["feasible"] else 0.0
    fallback_reason = None
    if not init_res["feasible"]:
        fallback_reason = "infeasible"
    elif init_tardiness > 0.0:
        fallback_reason = "tardy_compare"

    fallback_attempted = False
    if fallback_reason is not None and time.time() < fallback_deadline:
        target_entry_fb = [int(blocks[i]["release_time"]) for i in range(n)]
        assignments_fb, n_forced_fb, n_unplaced_fb = place_initial(
            prob_info, F, bays,
            target_entry_fb, target_orient,
            w1, w2, w3,
            fallback_deadline,
        )
        fb_res, fb_sol = evaluate_solution(prob_info, assignments_fb)
        fb_obj = float(fb_res["objective"]) if fb_res["feasible"] else float("inf")
        selected = fb_obj < init_obj
        fallback_attempted = True
        _emit("athena.init.fallback",
              elapsed=round(time.time() - t_start, 3),
              feasible=bool(fb_res["feasible"]),
              stage=str(fb_res.get("stage")),
              objective=fb_obj,
              n_forced=n_forced_fb,
              n_unplaced=n_unplaced_fb,
              reason=fallback_reason,
              selected=selected)
        if selected:
            assignments = assignments_fb
            init_obj = fb_obj
            init_sol = fb_sol
            init_res = fb_res

    if not init_res["feasible"] and time.time() < hard_deadline - safety:
        rep_assign, rep_res, rep_sol, rep_count = repair_conflict_closure(
            prob_info, F, bays, assignments, w1, w2, w3, repair_deadline,
        )
        rep_obj = float(rep_res["objective"]) if rep_res["feasible"] else float("inf")
        _emit("athena.fallback.repair" if fallback_attempted else "athena.init.repair",
              elapsed=round(time.time() - t_start, 3),
              feasible=bool(rep_res["feasible"]),
              stage=str(rep_res.get("stage")),
              objective=rep_obj,
              repaired=rep_count)
        if rep_res["feasible"]:
            assignments = rep_assign
            init_obj = rep_obj
            init_sol = rep_sol
            init_res = rep_res

    # Hard safety net: if both placement/repair passes failed, build a
    # boundary-safe serial solution. This may be tardy, but it never creates
    # an invalid coordinate and keeps SA in the feasible region.
    if not init_res["feasible"]:
        if emergency_assign is not None:
            all_forced = emergency_assign
            missing_forced = emergency_missing
            af_res = emergency_res
            af_sol = emergency_sol
            af_obj = emergency_obj
            af_source = "emergency"
        else:
            all_forced, missing_forced = build_safe_serial_assignments(
                prob_info, F, bays,
            )
            af_res, af_sol = evaluate_solution(prob_info, all_forced)
            af_obj = float(af_res["objective"]) if af_res["feasible"] else float("inf")
            af_source = "late"
        _emit("athena.init.all_forced",
              elapsed=round(time.time() - t_start, 3),
              feasible=bool(af_res["feasible"]),
              stage=str(af_res.get("stage")),
              objective=af_obj,
              missing=missing_forced,
              source=af_source)
        if af_res["feasible"] or af_obj < init_obj:
            assignments = all_forced
            init_obj = af_obj
            init_sol = af_sol
            init_res = af_res

    # Phase 5 -- parallel multi-start SA.
    # The initial solution above was built once in the main process. The
    # remaining budget is spent on up to 4 independent SA workers (contest
    # server allows 4 cores / 400% CPU). Workers stop at `sa_deadline`; the
    # main process gathers their results no later than `gather_deadline`
    # (< hard_deadline) so the total wall time stays within `timelimit`.
    max_workers = min(4, os.cpu_count() or 1)
    # Optional override (A/B testing / tuning). OGC2026_SA_WORKERS caps the
    # worker count; setting it to 1 forces the single-start path. Never exceeds
    # the contest's 4-core limit. OGC2026_SA_BASE_SEED makes runs reproducible
    # and lets repeats use independent randomness.
    _wenv = os.environ.get("OGC2026_SA_WORKERS")
    if _wenv:
        try:
            max_workers = max(1, min(4, int(_wenv)))
        except ValueError:
            pass
    base_seed = 12345
    _senv = os.environ.get("OGC2026_SA_BASE_SEED")
    if _senv:
        try:
            base_seed = int(_senv)
            random.seed(base_seed)
        except ValueError:
            pass
    # OGC2026_SA_PROFILE_MODE: "diverse" (default, the SA_PROFILES mix) or
    # "uniform" (all workers run the balanced profile, different seeds only ->
    # pure multi-start). Used for A/B tuning of the parallel design.
    profiles_override = None
    if os.environ.get("OGC2026_SA_PROFILE_MODE", "").lower() == "uniform":
        profiles_override = [_DEFAULT_PROFILE]
    remaining = sa_deadline - time.time()

    best_assign = None
    best_sol = init_sol
    best_obj = float("inf")
    iters = 0
    improvements = 0

    # Only fan out to processes when there is more than one core AND enough
    # remaining budget to amortise spawn overhead; otherwise run single-start.
    use_parallel = init_res["feasible"] and max_workers >= 2 and remaining > 2.0

    if use_parallel:
        ev_base = os.environ.get("OGC2026_EVENT_LOG")
        _emit("athena.parallel_sa.start",
              n_workers=max_workers, remaining=round(remaining, 3),
              worker_deadline=round(sa_deadline - t_start, 3),
              gather_deadline=round(gather_deadline - t_start, 3),
              mode="batched" if time_tier == "long" else "single")
        current_assign = assignments
        batch_idx = 0
        all_worker_results = []
        while time.time() < sa_deadline and current_assign is not None:
            if time_tier == "long":
                batch_deadline = min(sa_deadline, time.time() + 90.0)
                if batch_deadline - time.time() < 30.0:
                    break
            else:
                batch_deadline = sa_deadline
            batch_idx += 1
            batch_gather = min(gather_deadline, batch_deadline + 1.0)
            batch_seed = (
                base_seed + batch_idx * 104729 if time_tier == "long" else base_seed
            )
            _emit("athena.parallel_sa.batch_start",
                  batch=batch_idx,
                  worker_deadline=round(batch_deadline - t_start, 3),
                  remaining=round(batch_deadline - time.time(), 3))
            try:
                p_assign, p_obj, worker_results = parallel_sa_multi_start(
                    prob_info, current_assign, w1, w2, w3,
                    batch_deadline, batch_gather, max_workers,
                    base_seed=batch_seed,
                    event_log_base=ev_base, profiles_override=profiles_override,
                )
            except Exception as exc:
                p_assign, p_obj, worker_results = None, float("inf"), []
                _emit("athena.parallel_sa.exception", batch=batch_idx,
                      error=repr(exc))
            all_worker_results.extend(worker_results)
            _emit("athena.parallel_sa.batch_done",
                  batch=batch_idx,
                  feasible=p_assign is not None, objective=p_obj,
                  n_results=len(worker_results),
                  n_feasible=sum(1 for r in worker_results if r.get("feasible")))
            if p_assign is not None:
                # Re-verify the winning worker's assignment with the official
                # checker in the main process before trusting it.
                res, sol = evaluate_solution(prob_info, p_assign)
                if res["feasible"] and float(res["objective"]) < best_obj:
                    best_assign = p_assign
                    best_sol = sol
                    best_obj = float(res["objective"])
                    current_assign = best_assign
                    _emit("athena.parallel_sa.batch_selected",
                          batch=batch_idx, objective=best_obj)
            if time_tier != "long":
                break
        _emit("athena.parallel_sa.done",
              feasible=best_assign is not None, objective=best_obj,
              n_results=len(all_worker_results),
              n_feasible=sum(1 for r in all_worker_results if r.get("feasible")),
              batches=batch_idx)

    if init_res["feasible"] and best_assign is None and time.time() < sa_deadline:
        # Fallback: single-start SA in the main process. Taken when only one
        # core is available, too little budget remained, or the parallel run
        # produced no usable feasible solution.
        best_assign, best_sol, best_obj, iters, improvements = sa_loop(
            prob_info, F, bays, w1, w2, w3, assignments, sa_deadline,
        )

    # If SA never found anything feasible but init was, return init.
    final_sol = best_sol
    final_obj = best_obj
    if best_obj == float("inf") and init_obj != float("inf"):
        final_sol = init_sol
        final_obj = init_obj

    _emit("algo.end",
          best_objective=final_obj,
          wall_time=round(time.time() - t_start, 3),
          has_solution=bool(final_sol.get("operations")),
          sa_iterations=iters,
          sa_improvements=improvements)
    _close_event_log()
    return final_sol
