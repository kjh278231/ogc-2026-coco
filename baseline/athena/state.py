"""Incremental assignment state and objective helpers for Athena SA."""
from __future__ import annotations

from dataclasses import dataclass

from utils import Bay

def _bay_workload_dict(assignments: dict, prob_info: dict, n_bays: int) -> list[float]:
    loads = [0.0] * n_bays
    for a in assignments.values():
        loads[a["bay_id"]] += prob_info["blocks"][a["block_id"]]["workload"]
    return loads


def _bay_weights(bays: list[Bay]) -> list[float]:
    """Official obj2 area weights u_j = avg_area / (W_j * H_j).

    Mirrors build_state's weighting so the construction phase optimises the
    same imbalance metric the SA + final objective uses. Uniform-area bays
    yield all 1.0 (no behavioural change); the benchmark suite is non-uniform,
    so this is active on every instance.
    """
    areas = [b.width * b.height for b in bays]
    avg = sum(areas) / max(1, len(areas))
    return [avg / a if a else 0.0 for a in areas]


# =============================================================================
# Incremental SA support: FastState, fast checks, pair caches.
# =============================================================================

@dataclass
class Assignment:
    """Mutable placement record. Mirrors the keys of the dict-format assignment
    that the rest of the algorithm uses, but is hashable-by-identity so we can
    mutate fields in place and snapshot only the deltas during SA.
    """
    block_id: int
    bay_id: int
    x: int
    y: int
    orient_idx: int
    entry_time: int
    exit_time: int


def assignment_dict_to_obj(d: dict) -> Assignment:
    return Assignment(
        block_id=int(d["block_id"]), bay_id=int(d["bay_id"]),
        x=int(d["x"]), y=int(d["y"]), orient_idx=int(d["orient_idx"]),
        entry_time=int(d["entry_time"]), exit_time=int(d["exit_time"]),
    )


def assignment_obj_to_dict(a: Assignment) -> dict:
    return {
        "block_id": a.block_id, "bay_id": a.bay_id,
        "x": a.x, "y": a.y, "orient_idx": a.orient_idx,
        "entry_time": a.entry_time, "exit_time": a.exit_time,
    }


@dataclass
class FastState:
    """Incrementally maintained SA state.

    bay_blocks[j]    : set of block_id placed in bay j
    bay_workload[j]  : sum of workload in bay j
    bay_weights[j]   : u_j = avg_bay_area / (W_j * H_j) -- official obj2 weight
    total_tardiness  : sum max(0, exit - due)            -- official obj1
    z2_imbalance     : max |u_a * load_a - u_b * load_b| -- official obj2
    total_pref_pen   : sum (s_max_i - prefs_i[bay_id_i]) -- official obj3
    objective        : w1*z1 + w2*z2 + w3*z3 (cached, recomputed by helpers)
    """
    assignments: dict          # block_id -> Assignment
    bay_blocks: list           # [bay_id] -> set[block_id]
    bay_workload: list         # [bay_id] -> float
    bay_weights: list          # [bay_id] -> float
    total_tardiness: float
    z2_imbalance: float
    total_pref_pen: float
    objective: float


def build_state(assignments_dict: dict, prob_info: dict, bays: list[Bay],
                w1: float, w2: float, w3: float) -> FastState:
    blocks = prob_info["blocks"]
    n_bays = len(bays)
    bay_areas = [b.width * b.height for b in bays]
    avg = sum(bay_areas) / n_bays
    bay_weights = [avg / a for a in bay_areas]

    state = FastState(
        assignments={bi: assignment_dict_to_obj(a) for bi, a in assignments_dict.items()},
        bay_blocks=[set() for _ in bays],
        bay_workload=[0.0] * n_bays,
        bay_weights=bay_weights,
        total_tardiness=0.0,
        z2_imbalance=0.0,
        total_pref_pen=0.0,
        objective=0.0,
    )
    for bi, a in state.assignments.items():
        state.bay_blocks[a.bay_id].add(bi)
        state.bay_workload[a.bay_id] += blocks[bi]["workload"]
        state.total_tardiness += max(0, a.exit_time - blocks[bi]["due_date"])
        prefs = blocks[bi]["bay_preferences"]
        state.total_pref_pen += max(prefs) - prefs[a.bay_id]
    state.z2_imbalance = _compute_z2(state.bay_workload, state.bay_weights)
    state.objective = w1 * state.total_tardiness + w2 * state.z2_imbalance + w3 * state.total_pref_pen
    return state


def state_to_assignments_dict(state: FastState) -> dict:
    return {bi: assignment_obj_to_dict(a) for bi, a in state.assignments.items()}


def _compute_z2(bay_workload: list, bay_weights: list) -> float:
    n = len(bay_workload)
    if n < 2:
        return 0.0
    best = 0.0
    for i in range(n):
        wi = bay_weights[i] * bay_workload[i]
        for j in range(i + 1, n):
            d = abs(wi - bay_weights[j] * bay_workload[j])
            if d > best:
                best = d
    return best


def snapshot_changed(state: FastState, changed_ids: set) -> dict:
    """Capture per-block snapshot (BEFORE the move) for rollback."""
    snap = {}
    for bi in changed_ids:
        if bi in state.assignments:
            a = state.assignments[bi]
            snap[bi] = Assignment(
                block_id=a.block_id, bay_id=a.bay_id,
                x=a.x, y=a.y, orient_idx=a.orient_idx,
                entry_time=a.entry_time, exit_time=a.exit_time,
            )
    return snap


def rollback_changed(state: FastState, snapshot: dict,
                     prob_info: dict, w1: float, w2: float, w3: float) -> None:
    """Restore assignments and bay-side state, then re-derive objective."""
    blocks = prob_info["blocks"]
    for bi, snap_a in snapshot.items():
        curr = state.assignments[bi]
        if curr.bay_id != snap_a.bay_id:
            state.bay_blocks[curr.bay_id].discard(bi)
            state.bay_blocks[snap_a.bay_id].add(bi)
            state.bay_workload[curr.bay_id] -= blocks[bi]["workload"]
            state.bay_workload[snap_a.bay_id] += blocks[bi]["workload"]
        # restore tardiness component (we'll recompute totals from scratch below)
        curr.bay_id = snap_a.bay_id
        curr.x = snap_a.x
        curr.y = snap_a.y
        curr.orient_idx = snap_a.orient_idx
        curr.entry_time = snap_a.entry_time
        curr.exit_time = snap_a.exit_time
    # Recompute tardiness and pref from scratch (cheap: only over changed ids)
    # but to keep totals consistent we rebuild from snapshot deltas.
    # Easier and safer: recompute the whole totals (O(n)) once per rollback.
    _recompute_obj_totals(state, prob_info, w1, w2, w3)


def _recompute_obj_totals(state: FastState, prob_info: dict,
                          w1: float, w2: float, w3: float) -> None:
    blocks = prob_info["blocks"]
    z1 = 0.0
    z3 = 0.0
    for bi, a in state.assignments.items():
        z1 += max(0, a.exit_time - blocks[bi]["due_date"])
        prefs = blocks[bi]["bay_preferences"]
        z3 += max(prefs) - prefs[a.bay_id]
    state.total_tardiness = z1
    state.total_pref_pen = z3
    state.z2_imbalance = _compute_z2(state.bay_workload, state.bay_weights)
    state.objective = w1 * z1 + w2 * state.z2_imbalance + w3 * z3


def apply_obj_delta(state: FastState, prob_info: dict, snapshot: dict,
                    changed_ids: set, w1: float, w2: float, w3: float) -> dict:
    """O(|changed_ids| + n_bays^2) objective update after a move.

    Returns a delta-breakdown dict (used by the accept rule + logging):
        new_obj   : state.objective after the move
        delta_obj : new_obj - obj_before
        delta_z1  : tardiness change
        delta_z2  : z2_imbalance change
        delta_z3  : preference penalty change
    """
    blocks = prob_info["blocks"]
    old_obj = state.objective
    old_z1 = state.total_tardiness
    old_z2 = state.z2_imbalance
    old_z3 = state.total_pref_pen
    for bi in changed_ids:
        a = state.assignments[bi]
        old = snapshot.get(bi)
        new_t = max(0, a.exit_time - blocks[bi]["due_date"])
        if old is not None:
            old_t = max(0, old.exit_time - blocks[bi]["due_date"])
            state.total_tardiness += (new_t - old_t)
        else:
            state.total_tardiness += new_t
        prefs = blocks[bi]["bay_preferences"]
        new_p = max(prefs) - prefs[a.bay_id]
        if old is not None:
            old_p = max(prefs) - prefs[old.bay_id]
            state.total_pref_pen += (new_p - old_p)
        else:
            state.total_pref_pen += new_p
    state.z2_imbalance = _compute_z2(state.bay_workload, state.bay_weights)
    state.objective = (w1 * state.total_tardiness
                       + w2 * state.z2_imbalance
                       + w3 * state.total_pref_pen)
    return {
        "new_obj": state.objective,
        "delta_obj": state.objective - old_obj,
        "delta_z1": state.total_tardiness - old_z1,
        "delta_z2": state.z2_imbalance - old_z2,
        "delta_z3": state.total_pref_pen - old_z3,
    }
