"""Simulated Annealing loop and acceptance policy for Athena."""
from __future__ import annotations

import math
import random
import time

from utils import Bay

from .events import _emit
from .fast_checks import fast_check_move
from .features import Features
from .moves import _apply_large_move, _do_medium_move, _do_small_move, _pick_random_block
from .solution import evaluate_solution
from .state import (
    FastState,
    apply_obj_delta,
    build_state,
    rollback_changed,
    snapshot_changed,
    state_to_assignments_dict,
)

def estimate_initial_temperature(state: FastState, prob_info: dict, F: Features,
                                  bays: list[Bay], w1: float, w2: float, w3: float,
                                  cache_pair: dict, cache_crane: dict,
                                  sample_count: int = 30,
                                  target_accept_prob: float = 0.05) -> float:
    """Sample random small/medium moves to estimate the worse-delta scale,
    then pick T0 so that the median worse move is accepted with probability
    `target_accept_prob`. Falls back to 1.0 when no worse delta is observed.
    """
    worse_deltas: list[float] = []
    for _ in range(sample_count):
        if not state.assignments:
            break
        bi = _pick_random_block(state)
        changed_ids = {bi}
        snap = snapshot_changed(state, changed_ids)
        if random.random() < 0.6:
            _do_small_move(state, prob_info, F, bi)
        else:
            label = _do_medium_move(state, prob_info, F, bays, bi)
            if label is None:
                rollback_changed(state, snap, prob_info, w1, w2, w3)
                continue
        if fast_check_move(prob_info, F, bays, state, snap, changed_ids,
                            cache_pair, cache_crane):
            delta_info = apply_obj_delta(state, prob_info, snap, changed_ids,
                                          w1, w2, w3)
            if delta_info["delta_obj"] > 0:
                worse_deltas.append(delta_info["delta_obj"])
        rollback_changed(state, snap, prob_info, w1, w2, w3)
    if not worse_deltas:
        return 1.0
    worse_deltas.sort()
    median_delta = worse_deltas[len(worse_deltas) // 2]
    if median_delta <= 0:
        return 1.0
    T0 = -median_delta / math.log(target_accept_prob)
    return max(1e-6, T0)


def _should_accept(delta_obj: float, delta_z1: float, T: float,
                    curr_obj: float,
                    tardiness_mult: float = 3.0,
                    hard_worse_ratio: float = 0.02,
                    hard_worse_floor: float = 1000.0) -> tuple[bool, float, float]:
    """Tardiness-aware Metropolis with hard worse-limit.

    Returns (accept?, effective_delta, accept_prob).
    """
    if delta_obj <= 0:
        return True, delta_obj, 1.0
    effective = delta_obj
    if delta_z1 > 0:
        effective = delta_obj * tardiness_mult
    base = curr_obj if curr_obj != float("inf") and curr_obj > 0 else hard_worse_floor
    hard_limit = max(hard_worse_floor, base * hard_worse_ratio)
    if effective > hard_limit:
        return False, effective, 0.0
    p = math.exp(-effective / max(T, 1e-9))
    return (random.random() < p), effective, p


def _adaptive_full_check_period(n_blocks: int) -> int:
    if n_blocks <= 50:
        return 20
    if n_blocks <= 150:
        return 50
    return 100

# =============================================================================
# Parallel multi-start SA
# =============================================================================
# After the initial solution is built once in the main process, the remaining
# time budget is spent running up to 4 *independent* SA workers in parallel
# (the contest server allows 4 CPU cores / 400% CPU). Each worker starts from a
# deepcopy of the same initial assignment but explores a different trajectory:
# distinct random seed, move mix, medium-move bay-change bias, and temperature
# schedule. The main process then keeps the best feasible result.
#
# Each profile's `move_probs` is the cumulative cutoff pair (p_small_cut,
# p_medium_cut): small if r < p_small_cut, medium if r < p_medium_cut, else
# large. Profile 0 ("balanced") reproduces the historical single-start mix
# (0.55 / 0.88), so running with a single worker behaves like the old solver.

SA_PROFILES: list[dict] = [
    {   # profile 0: balanced  (small 55% / medium 33% / large 12%)
        "name": "balanced",
        "move_probs": (0.55, 0.88),
        "bay_change_prob": 0.50,
        "cooling": 0.998,
        "t0_scale": 1.0,
    },
    {   # profile 1: large-repair focused (small 40% / medium 25% / large 35%)
        "name": "large_repair",
        "move_probs": (0.40, 0.65),
        "bay_change_prob": 0.50,
        "cooling": 0.997,
        "t0_scale": 1.3,
    },
    {   # profile 2: bay-reassignment focused (small 35% / medium 50% / large 15%)
        "name": "bay_reassign",
        "move_probs": (0.35, 0.85),
        "bay_change_prob": 0.75,
        "cooling": 0.998,
        "t0_scale": 1.0,
    },
    {   # profile 3: local-position focused (small 70% / medium 20% / large 10%)
        "name": "local_position",
        "move_probs": (0.70, 0.90),
        "bay_change_prob": 0.25,
        "cooling": 0.999,
        "t0_scale": 0.7,
    },
]

_DEFAULT_PROFILE = SA_PROFILES[0]


def _resolve_profile(profile: dict | None) -> dict:
    """Fill in any missing keys from the balanced default so callers may pass
    a partial profile dict (or None)."""
    if not profile:
        return dict(_DEFAULT_PROFILE)
    merged = dict(_DEFAULT_PROFILE)
    merged.update(profile)
    return merged

def sa_loop(prob_info: dict, F: Features, bays: list[Bay],
            w1: float, w2: float, w3: float,
            assignments: dict, deadline: float,
            full_check_period: int | None = None,
            profile: dict | None = None
            ) -> tuple[dict, dict, float, int, int]:
    """Stabilized SA loop with adaptive T0, no reheat, tardiness-aware accept
    rule, hard worse-limit, adaptive periodic full check, and rollback to the
    last full-valid state (not to global best) on detected drift.

    Incremental check covers small/medium moves; large move keeps its full
    destroy-repair + full check_feasibility path.

    `profile` (see SA_PROFILES) tunes the move mix, the medium-move bay-change
    bias, the temperature schedule, and the T0 scale. When None the balanced
    profile is used, which reproduces the historical single-start behaviour.
    """
    prof = _resolve_profile(profile)
    p_small_cut, p_medium_cut = prof["move_probs"]
    bay_change_prob = prof["bay_change_prob"]
    profile_cooling = prof["cooling"]
    t0_scale = prof["t0_scale"]

    state = build_state(assignments, prob_info, bays, w1, w2, w3)
    cache_pair: dict = {}
    cache_crane: dict = {}
    n_blocks = len(state.assignments)
    base_full_period = full_check_period or _adaptive_full_check_period(n_blocks)
    current_full_period = base_full_period

    # ---- initial baseline via full checker -------------------------------
    init_dict = state_to_assignments_dict(state)
    init_res, init_sol = evaluate_solution(prob_info, init_dict)
    if init_res["feasible"]:
        best_obj = float(init_res["objective"])
        curr_obj = best_obj
        best_sol = init_sol
        best_assign = init_dict
        state.objective = best_obj
        last_full_valid_assign = {k: dict(v) for k, v in init_dict.items()}
        last_full_valid_obj = best_obj
    else:
        best_obj = float("inf")
        curr_obj = float("inf")
        best_sol = init_sol
        best_assign = init_dict
        last_full_valid_assign = None
        last_full_valid_obj = float("inf")

    # ---- adaptive T0 estimate via short sampling pass --------------------
    T0 = estimate_initial_temperature(state, prob_info, F, bays, w1, w2, w3,
                                       cache_pair, cache_crane)
    T0 = max(1e-6, T0 * t0_scale)
    T_min = max(1e-6, T0 * 0.001)
    cooling = profile_cooling
    T = T0
    _emit("sa.temperature.init",
          T0=T0, T_min=T_min, cooling=cooling,
          base_full_period=base_full_period,
          n_blocks=n_blocks, profile=prof["name"])

    iters = 0
    improvements = 0
    accepts = 0
    worse_accepts = 0
    worse_z1_accepts = 0
    fast_rejects = 0
    worse_limit_rejects = 0
    full_checks = 0
    mismatches = 0
    rollback_best_count = 0
    rollback_last_valid_count = 0
    last_improvement_iter = 0

    while time.time() < deadline:
        iters += 1

        r = random.random()
        if r < p_small_cut:
            move_type = "small"
        elif r < p_medium_cut:
            move_type = "medium"
        else:
            move_type = "large"

        # ============================================================
        # LARGE MOVE: full destroy-repair + full checker (unchanged)
        # ============================================================
        if move_type == "large":
            dict_assign = state_to_assignments_dict(state)
            _apply_large_move(dict_assign, prob_info, F, bays, deadline,
                              w1, w2, w3)
            res, sol = evaluate_solution(prob_info, dict_assign)
            obj = float(res["objective"]) if res["feasible"] else float("inf")

            accept = False
            if obj < curr_obj:
                accept = True
            elif obj != float("inf"):
                if random.random() < math.exp(-(obj - curr_obj) / max(T, 1e-9)):
                    accept = True

            if accept:
                accepts += 1
                if obj > curr_obj:
                    worse_accepts += 1
                curr_obj = obj
                state = build_state(dict_assign, prob_info, bays, w1, w2, w3)
                state.objective = obj
                # large move was fully checked just now; treat as a fresh
                # last-full-valid checkpoint
                last_full_valid_assign = {k: dict(v) for k, v in dict_assign.items()}
                last_full_valid_obj = obj
                if obj < best_obj:
                    best_obj = obj
                    best_sol = sol
                    best_assign = {k: dict(v) for k, v in dict_assign.items()}
                    improvements += 1
                    last_improvement_iter = iters
                    _emit("sa.improvement",
                          iteration=iters, move_type=move_type,
                          objective=obj)

            T = max(T_min, T * cooling)
            continue

        # ============================================================
        # SMALL / MEDIUM: incremental fast path
        # ============================================================
        bi_pick = _pick_random_block(state)
        changed_ids = {bi_pick}
        snapshot = snapshot_changed(state, changed_ids)
        if move_type == "small":
            move_sublabel = _do_small_move(state, prob_info, F, bi_pick)
        else:
            move_sublabel = _do_medium_move(state, prob_info, F, bays, bi_pick,
                                            bay_change_prob)
            if move_sublabel is None:
                # noop move (bay_fit empty, etc.) -- skip without counting
                T = max(T_min, T * cooling)
                continue

        if not fast_check_move(prob_info, F, bays, state, snapshot,
                                changed_ids, cache_pair, cache_crane):
            fast_rejects += 1
            rollback_changed(state, snapshot, prob_info, w1, w2, w3)
            T = max(T_min, T * cooling)
            continue

        delta_info = apply_obj_delta(state, prob_info, snapshot, changed_ids,
                                      w1, w2, w3)
        new_obj = delta_info["new_obj"]
        delta_obj = delta_info["delta_obj"]
        delta_z1 = delta_info["delta_z1"]

        accept, effective, accept_prob = _should_accept(
            delta_obj, delta_z1, T, curr_obj,
        )
        if not accept and delta_obj > 0 and effective > 0:
            # distinguish hard-limit reject from probabilistic reject
            base = curr_obj if curr_obj != float("inf") and curr_obj > 0 else 1000.0
            hard_limit = max(1000.0, base * 0.02)
            if effective > hard_limit:
                worse_limit_rejects += 1
                _emit("sa.reject.worse_limit",
                      iteration=iters, T=round(T, 4),
                      curr_obj=curr_obj, new_obj=new_obj,
                      delta_obj=delta_obj, delta_tardiness=delta_z1,
                      effective_delta=effective, hard_limit=hard_limit,
                      move_type=move_sublabel,
                      changed_blocks=list(changed_ids))

        if accept:
            accepts += 1
            if delta_obj > 0:
                worse_accepts += 1
                if delta_z1 > 0:
                    worse_z1_accepts += 1
                    _emit("sa.accept.worse_tardiness",
                          iteration=iters, T=round(T, 4),
                          curr_obj=curr_obj, new_obj=new_obj,
                          delta_obj=delta_obj, delta_tardiness=delta_z1,
                          accept_prob=round(accept_prob, 6),
                          changed_blocks=list(changed_ids),
                          move_type=move_sublabel)
                else:
                    _emit("sa.accept.worse",
                          iteration=iters, T=round(T, 4),
                          curr_obj=curr_obj, new_obj=new_obj,
                          delta_obj=delta_obj, delta_tardiness=delta_z1,
                          accept_prob=round(accept_prob, 6),
                          changed_blocks=list(changed_ids),
                          move_type=move_sublabel)
            curr_obj = new_obj

            # ---- best-objective candidate: ALWAYS verify with full checker
            if new_obj < best_obj:
                dict_assign = state_to_assignments_dict(state)
                res, sol = evaluate_solution(prob_info, dict_assign)
                full_checks += 1
                if res["feasible"]:
                    obj_full = float(res["objective"])
                    last_full_valid_assign = {k: dict(v) for k, v in dict_assign.items()}
                    last_full_valid_obj = obj_full
                    if obj_full < best_obj:
                        best_obj = obj_full
                        best_sol = sol
                        best_assign = dict_assign
                        improvements += 1
                        last_improvement_iter = iters
                        _emit("sa.full_check.best",
                              iteration=iters, move_type=move_sublabel,
                              objective=obj_full, fast_obj=new_obj)
                        if abs(obj_full - new_obj) > 1e-6:
                            _emit("sa.fast_full_mismatch",
                                  iteration=iters,
                                  fast=new_obj, full=obj_full,
                                  scenario="best_check_drift")
                            state.objective = obj_full
                            curr_obj = obj_full
                else:
                    # fast checker passed but official rejected -- rollback to
                    # the per-block snapshot (closest valid state we can recover)
                    mismatches += 1
                    rollback_changed(state, snapshot, prob_info, w1, w2, w3)
                    curr_obj = state.objective
                    _emit("sa.fast_full_mismatch",
                          iteration=iters, move_type=move_sublabel,
                          fast_feasible=True, full_feasible=False,
                          fast_obj=new_obj,
                          changed_blocks=list(changed_ids),
                          action="rollback_snapshot")
                    # adapt: shrink period after a mismatch
                    current_full_period = max(10, current_full_period // 2)
                    T = max(T_min, T * cooling)
                    continue

            # ---- periodic full-state revalidation -------------------
            if accepts - 0 > 0 and (accepts % current_full_period == 0):
                dict_assign = state_to_assignments_dict(state)
                res, _sol_pf = evaluate_solution(prob_info, dict_assign)
                full_checks += 1
                if res["feasible"]:
                    obj_full = float(res["objective"])
                    last_full_valid_assign = {k: dict(v) for k, v in dict_assign.items()}
                    last_full_valid_obj = obj_full
                    if abs(obj_full - state.objective) > 1e-6:
                        _emit("sa.fast_full_mismatch",
                              iteration=iters,
                              fast=state.objective, full=obj_full,
                              scenario="periodic_drift")
                        state.objective = obj_full
                        curr_obj = obj_full
                    # back off period after a clean check
                    if current_full_period < base_full_period:
                        current_full_period = min(base_full_period,
                                                   current_full_period + 5)
                    _emit("sa.periodic_full_check",
                          iteration=iters, feasible=True,
                          period=current_full_period)
                else:
                    mismatches += 1
                    if last_full_valid_assign is not None:
                        rollback_last_valid_count += 1
                        state = build_state(last_full_valid_assign, prob_info,
                                             bays, w1, w2, w3)
                        state.objective = last_full_valid_obj
                        curr_obj = last_full_valid_obj
                        _emit("sa.rollback.last_valid",
                              iteration=iters,
                              restored_obj=last_full_valid_obj,
                              new_period=max(10, current_full_period // 2))
                    elif best_obj != float("inf"):
                        rollback_best_count += 1
                        state = build_state(best_assign, prob_info, bays,
                                             w1, w2, w3)
                        state.objective = best_obj
                        curr_obj = best_obj
                        _emit("sa.rollback.best",
                              iteration=iters, restored_obj=best_obj)
                    # tighten cadence after a drift event
                    current_full_period = max(10, current_full_period // 2)
        else:
            rollback_changed(state, snapshot, prob_info, w1, w2, w3)

        T = max(T_min, T * cooling)

    _emit("sa.complete",
          iterations=iters, improvements=improvements,
          accepts=accepts, worse_accepts=worse_accepts,
          worse_z1_accepts=worse_z1_accepts,
          fast_rejects=fast_rejects,
          worse_limit_rejects=worse_limit_rejects,
          full_checks=full_checks, mismatches=mismatches,
          rollback_last_valid=rollback_last_valid_count,
          rollback_best=rollback_best_count,
          last_improvement_iter=last_improvement_iter,
          best_objective=best_obj,
          final_T=round(T, 6), T0=T0,
          pair_cache_size=len(cache_pair),
          crane_cache_size=len(cache_crane))
    return best_assign, best_sol, best_obj, iters, improvements
