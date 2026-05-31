"""Compatibility shim for the Athena solver.

The public contestant contract remains `algorithm(prob_info, timelimit)`.  The
implementation lives in the `athena` package so individual solver subsystems can
be edited and documented without keeping one very large module open.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import utils  # noqa: E402
from utils import (  # noqa: E402
    Bay,
    Block,
    _bounding_box,
    _poly_from_verts,
    _resolve_layers,
    check_collisions,
    check_entry,
    check_exit,
    check_feasibility,
)

from athena.entrypoint import algorithm, _silence_stdout
from athena.events import _close_event_log, _emit, _init_event_log
from athena.fast_checks import (
    _CRANE_CACHE_CAP,
    _PAIR_CACHE_CAP,
    _cap_cache,
    _coll_key,
    _crane_key,
    _crane_obstructs,
    _is_present_at_entry_of,
    _is_present_at_exit_of,
    _pair_collides,
    fast_check_move,
)
from athena.features import Features, precompute_features, smooth_time_windows
from athena.moves import (
    _apply_large_move,
    _apply_medium_move_state,
    _apply_small_move_state,
    _do_medium_move,
    _do_small_move,
    _pick_random_block,
)
from athena.parallel import _sa_worker, parallel_sa_multi_start
from athena.placement import (
    _candidate_positions,
    _empty_bay_entry,
    _find_earliest_slot,
    _force_place,
    _placement_score,
    _time_overlap,
    place_initial,
    rank_bays_for_block,
)
from athena.sa import (
    SA_PROFILES,
    _DEFAULT_PROFILE,
    _adaptive_full_check_period,
    _resolve_profile,
    _should_accept,
    estimate_initial_temperature,
    sa_loop,
)
from athena.solution import assignments_to_solution, evaluate_solution
from athena.state import (
    Assignment,
    FastState,
    _bay_weights,
    _bay_workload_dict,
    _compute_z2,
    _recompute_obj_totals,
    apply_obj_delta,
    assignment_dict_to_obj,
    assignment_obj_to_dict,
    build_state,
    rollback_changed,
    snapshot_changed,
    state_to_assignments_dict,
)

__all__ = [name for name in globals() if not name.startswith("__")]
