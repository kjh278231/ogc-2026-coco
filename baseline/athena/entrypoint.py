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
from .placement import _force_place, place_initial
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

    # Phase 2
    target_entry, target_orient = smooth_time_windows(prob_info, F)
    _emit("athena.smoothing.done", elapsed=round(time.time() - t_start, 3))

    # Phase 4 (uses phase 3 ranker internally)
    init_deadline = min(hard_deadline - safety, t_start + max(2.0, timelimit * 0.30))
    assignments, n_forced = place_initial(
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
          n_forced=n_forced)

    # If the initial pipeline failed (smoothing pushed everyone into the same
    # window, etc.), retry with target_entry == release_time as a safety net.
    if not init_res["feasible"]:
        target_entry_fb = [int(blocks[i]["release_time"]) for i in range(n)]
        assignments_fb, n_forced_fb = place_initial(
            prob_info, F, bays,
            target_entry_fb, target_orient,
            w1, w2, w3,
            min(hard_deadline - safety, t_start + max(4.0, timelimit * 0.55)),
        )
        fb_res, fb_sol = evaluate_solution(prob_info, assignments_fb)
        fb_obj = float(fb_res["objective"]) if fb_res["feasible"] else float("inf")
        _emit("athena.init.fallback",
              elapsed=round(time.time() - t_start, 3),
              feasible=bool(fb_res["feasible"]),
              stage=str(fb_res.get("stage")),
              objective=fb_obj,
              n_forced=n_forced_fb)
        if fb_obj < init_obj:
            assignments = assignments_fb
            init_obj = fb_obj
            init_sol = fb_sol
            init_res = fb_res

    # Hard safety net: if both place_initial passes failed, do an all-forced
    # pass that places every block into an empty-bay window via _force_place.
    # By construction this is feasible (each block alone in the bay during
    # its time interval).
    if not init_res["feasible"]:
        all_forced = {}
        f_bay_schedule = [[] for _ in bays]
        edd_order = sorted(range(n), key=lambda i: blocks[i]["due_date"])
        for bi in edd_order:
            bid, fx, fy, foi, fe, fe_t = _force_place(bi, prob_info, F, bays, f_bay_schedule)
            f_bay_schedule[bid].append((fe, fe_t))
            all_forced[bi] = {
                "block_id": bi, "bay_id": bid,
                "x": int(fx), "y": int(fy), "orient_idx": foi,
                "entry_time": int(fe), "exit_time": int(fe_t),
            }
        af_res, af_sol = evaluate_solution(prob_info, all_forced)
        af_obj = float(af_res["objective"]) if af_res["feasible"] else float("inf")
        _emit("athena.init.all_forced",
              elapsed=round(time.time() - t_start, 3),
              feasible=bool(af_res["feasible"]),
              stage=str(af_res.get("stage")),
              objective=af_obj)
        if af_obj < init_obj:
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
    sa_deadline = min(hard_deadline - safety, t_start + max(4.0, timelimit * 0.92))
    gather_margin = min(0.5, max(0.05, timelimit * 0.01))
    gather_deadline = hard_deadline - gather_margin
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
    use_parallel = max_workers >= 2 and remaining > 2.0

    if use_parallel:
        ev_base = os.environ.get("OGC2026_EVENT_LOG")
        _emit("athena.parallel_sa.start",
              n_workers=max_workers, remaining=round(remaining, 3),
              worker_deadline=round(sa_deadline - t_start, 3),
              gather_deadline=round(gather_deadline - t_start, 3))
        try:
            p_assign, p_obj, worker_results = parallel_sa_multi_start(
                prob_info, assignments, w1, w2, w3,
                sa_deadline, gather_deadline, max_workers, base_seed=base_seed,
                event_log_base=ev_base, profiles_override=profiles_override,
            )
        except Exception as exc:
            p_assign, p_obj, worker_results = None, float("inf"), []
            _emit("athena.parallel_sa.exception", error=repr(exc))
        _emit("athena.parallel_sa.done",
              feasible=p_assign is not None, objective=p_obj,
              n_results=len(worker_results),
              n_feasible=sum(1 for r in worker_results if r.get("feasible")))
        if p_assign is not None:
            # Re-verify the winning worker's assignment with the official
            # checker in the main process before trusting it.
            res, sol = evaluate_solution(prob_info, p_assign)
            if res["feasible"]:
                best_assign = p_assign
                best_sol = sol
                best_obj = float(res["objective"])

    if best_assign is None and time.time() < sa_deadline:
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
