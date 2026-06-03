"""State-mutating move operators for Athena SA."""
from __future__ import annotations

import math
import random
import time

from utils import Bay, Block

from .features import Features
from .placement import (
    _candidate_positions,
    _find_earliest_slot,
    _placement_score,
    _safe_fallback_place,
    rank_bays_for_block,
)
from .state import _bay_weights
from .state import FastState

# --------- State-mutating move variants ----------------------------------

def _apply_small_move_state(state: FastState, prob_info: dict, F: Features) -> set:
    bi = random.choice(list(state.assignments.keys()))
    a = state.assignments[bi]
    blk = prob_info["blocks"][bi]
    if random.random() < 0.6:
        delta = random.choice([-5, -3, -2, -1, 1, 2, 3, 5])
        new_e = max(int(blk["release_time"]), a.entry_time + delta)
        a.entry_time = new_e
        a.exit_time = new_e + int(blk["processing_time"])
    else:
        n_or = len(blk["shape"])
        if n_or > 1:
            new_oi = random.randrange(n_or)
            a.orient_idx = new_oi
            anchor = F.safe_anchor.get((bi, new_oi, a.bay_id))
            if anchor is not None:
                a.x, a.y = anchor
            else:
                bb = F.aabb.get((bi, new_oi))
                if bb is not None:
                    a.x = max(0, int(math.ceil(-bb[0])))
                    a.y = max(0, int(math.ceil(-bb[1])))
    return {bi}


def _apply_medium_move_state(state: FastState, prob_info: dict, F: Features,
                              bays: list[Bay]) -> set:
    bi = random.choice(list(state.assignments.keys()))
    a = state.assignments[bi]
    blk = prob_info["blocks"][bi]
    fit = F.bay_fit.get((bi, a.orient_idx), [])
    if not fit:
        return set()
    if random.random() < 0.5 and len(fit) > 1:
        alt = [b for b in fit if b != a.bay_id] or fit
        new_bid = random.choice(alt)
        old_bid = a.bay_id
        state.bay_blocks[old_bid].discard(bi)
        state.bay_blocks[new_bid].add(bi)
        state.bay_workload[old_bid] -= blk["workload"]
        state.bay_workload[new_bid] += blk["workload"]
        a.bay_id = new_bid
        anchor = F.safe_anchor.get((bi, a.orient_idx, new_bid))
        if anchor is not None:
            a.x, a.y = anchor
        else:
            bb = F.aabb.get((bi, a.orient_idx))
            if bb is not None:
                a.x = max(0, int(math.ceil(-bb[0])))
                a.y = max(0, int(math.ceil(-bb[1])))
    else:
        bay = bays[a.bay_id]
        bb = F.aabb.get((bi, a.orient_idx))
        if bb is None:
            return {bi}
        bounds = F.anchor_bounds.get((bi, a.orient_idx, a.bay_id))
        if bounds is not None:
            min_x, max_x, min_y, max_y = bounds
        else:
            min_x = 0
            min_y = 0
            max_x = int(bay.width - (bb[2] - bb[0]))
            max_y = int(bay.height - (bb[3] - bb[1]))
        dx = random.choice([-3, -2, -1, 1, 2, 3])
        dy = random.choice([-2, -1, 1, 2])
        a.x = max(min_x, min(max_x, a.x + dx))
        a.y = max(min_y, min(max_y, a.y + dy))
    return {bi}


# --------- Large-move retained from previous version (operates on dict). --
# Used through a state<->dict bridge in sa_loop_fast. ------------------

def _apply_large_move(assignments: dict, prob_info: dict, F: Features,
                       bays: list[Bay], deadline: float,
                       w1: float, w2: float, w3: float) -> None:
    """Destroy tardy / overloaded blocks and sweep-repair them."""
    blocks = prob_info["blocks"]
    n = len(blocks)
    tardy = []
    for bi, a in assignments.items():
        lateness = a["exit_time"] - blocks[bi]["due_date"]
        if lateness > 0:
            tardy.append((lateness, bi))
    if not tardy:
        k = max(1, n // 15)
        seed_blocks = random.sample(list(assignments.keys()), min(k, n))
    else:
        tardy.sort(reverse=True)
        destroy_size = max(1, min(len(tardy), n // 10))
        pool = [bi for _, bi in tardy[:max(destroy_size * 3, destroy_size)]]
        keep_worst = max(1, destroy_size // 2)
        seed_blocks = [bi for _, bi in tardy[:keep_worst]]
        remaining = [bi for bi in pool if bi not in seed_blocks]
        if remaining and len(seed_blocks) < destroy_size:
            seed_blocks.extend(random.sample(
                remaining, min(len(remaining), destroy_size - len(seed_blocks))
            ))

    # Destroy the tardy seeds plus nearby same-bay blockers. Reordering only
    # the tardy blocks rarely opens an earlier slot when the bay timeline is
    # already dense.
    destroyed = set(seed_blocks)
    destroy_limit = max(
        len(destroyed),
        min(n // 6, len(seed_blocks) + max(2, len(seed_blocks) // 2)),
    )
    for bi in list(seed_blocks):
        if len(destroyed) >= destroy_limit:
            break
        a = assignments[bi]
        r = int(blocks[bi]["release_time"])
        same_bay = []
        for bj, aj in assignments.items():
            if bj in destroyed or aj["bay_id"] != a["bay_id"]:
                continue
            if aj["entry_time"] <= a["exit_time"] and aj["exit_time"] >= r:
                same_bay.append((abs(aj["entry_time"] - a["entry_time"]), bj))
        same_bay.sort()
        for _, bj in same_bay:
            destroyed.add(bj)
            if len(destroyed) >= destroy_limit:
                break

    # rebuild local bay state without destroyed blocks
    bay_placed: list[list[Block]] = [[] for _ in bays]
    bay_schedule: list[list[tuple[int, int]]] = [[] for _ in bays]
    bay_loads: list[float] = [0.0] * len(bays)
    # process kept blocks in entry order to mirror sweep
    kept_order = sorted(
        (bi for bi in assignments if bi not in destroyed),
        key=lambda bi: assignments[bi]["entry_time"],
    )
    for bi in kept_order:
        a = assignments[bi]
        b = Block(block_id=bi, block_data=blocks[bi],
                  x=a["x"], y=a["y"], orient_idx=a["orient_idx"])
        bay_placed[a["bay_id"]].append(b)
        bay_schedule[a["bay_id"]].append((a["entry_time"], a["exit_time"]))
        bay_loads[a["bay_id"]] += blocks[bi]["workload"]

    # Reinsert urgent/tardy blocks first; this is where the temporal
    # neighborhood destroy gets a chance to change bay order.
    def _repair_key(bi: int) -> tuple[int, int, int]:
        blk = blocks[bi]
        old = assignments[bi]
        slack = (int(blk["due_date"]) - int(blk["release_time"])
                 - int(blk["processing_time"]))
        old_late = max(0, int(old["exit_time"]) - int(blk["due_date"]))
        return (slack, int(blk["due_date"]), -old_late)

    repair_order = sorted(destroyed, key=_repair_key)
    bay_weights = _bay_weights(bays)
    for bi in repair_order:
        if deadline is not None and time.time() >= deadline:
            break
        blk = blocks[bi]
        r = int(blk["release_time"])
        p = int(blk["processing_time"])
        due = int(blk["due_date"])
        prefs = blk["bay_preferences"]
        s_max = max(prefs)
        ranked = rank_bays_for_block(prob_info, F, bays, bi, bay_loads, w1, w2, w3,
                                      bay_weights, bay_schedule, r, p, due)
        best_score = float("inf")
        best = None
        bay_cap = max(3, min(5, len(bays)))
        for _, bid, oi in ranked[:bay_cap]:
            bay = bays[bid]
            blk_bb = F.aabb[(bi, oi)]
            cands = _candidate_positions(
                bay, blk_bb, bay_placed[bid],
                F.anchor_bounds.get((bi, oi, bid)))[:10]
            for cx, cy in cands:
                new_blk = Block(block_id=bi, block_data=blk, x=cx, y=cy, orient_idx=oi)
                if not bay.contains_block(new_blk):
                    continue
                e, e_t = _find_earliest_slot(bay, bay_placed[bid], bay_schedule[bid],
                                              new_blk, r, p, deadline, F)
                if e is None:
                    continue
                tard = max(0, e_t - due)
                score = _placement_score(tard, blk["workload"], bay_loads, bid,
                                          s_max - prefs[bid], cy + blk_bb[3],
                                          w1, w2, w3, bay_weights)
                if score < best_score:
                    best_score = score
                    best = (bid, cx, cy, oi, e, e_t)
        if best is None:
            best = _safe_fallback_place(
                bi, prob_info, F, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, bay_weights,
                deadline=deadline, earliest_entry=r,
            )
        if best is None:
            # Keep the original assignment rather than inventing an invalid
            # repair. The full checker will decide whether the large move is
            # worth accepting.
            old = assignments[bi]
            best = (old["bay_id"], old["x"], old["y"], old["orient_idx"],
                    old["entry_time"], old["exit_time"])
        bid, cx, cy, oi, e, e_t = best
        bay_placed[bid].append(Block(block_id=bi, block_data=blk, x=cx, y=cy, orient_idx=oi))
        bay_schedule[bid].append((e, e_t))
        bay_loads[bid] += blk["workload"]
        assignments[bi] = {
            "block_id": bi, "bay_id": bid,
            "x": int(cx), "y": int(cy), "orient_idx": oi,
            "entry_time": int(e), "exit_time": int(e_t),
        }


# =============================================================================
# Move helpers and SA components
# =============================================================================

def _pick_random_block(state: FastState) -> int:
    """Cheap random block-id picker. Avoids constructing a fresh list every
    iteration on large instances."""
    # dict.keys() in CPython 3.7+ is insertion-ordered. Sampling an index then
    # walking the dict view is O(n); we accept the cost as block counts are
    # small (<=200 in the benchmarks).
    keys = list(state.assignments.keys())
    return keys[random.randrange(len(keys))]


def _do_small_move(state: FastState, prob_info: dict, F: Features,
                    bi: int) -> str:
    """In-place small move on block `bi`. Returns a sub-label for logging."""
    a = state.assignments[bi]
    blk = prob_info["blocks"][bi]
    if random.random() < 0.6:
        delta = random.choice([-5, -3, -2, -1, 1, 2, 3, 5])
        new_e = max(int(blk["release_time"]), a.entry_time + delta)
        a.entry_time = new_e
        a.exit_time = new_e + int(blk["processing_time"])
        return "small_entry_shift"
    n_or = len(blk["shape"])
    if n_or > 1:
        new_oi = random.randrange(n_or)
        a.orient_idx = new_oi
        anchor = F.safe_anchor.get((bi, new_oi, a.bay_id))
        if anchor is not None:
            a.x, a.y = anchor
        else:
            bb = F.aabb.get((bi, new_oi))
            if bb is not None:
                a.x = max(0, int(math.ceil(-bb[0])))
                a.y = max(0, int(math.ceil(-bb[1])))
        return "small_orient_swap"
    return "small_noop"


def _do_medium_move(state: FastState, prob_info: dict, F: Features,
                     bays: list[Bay], bi: int,
                     bay_change_prob: float = 0.5) -> str | None:
    """In-place medium move on block `bi`. Returns sub-label or None on noop.

    `bay_change_prob` biases the choice between a bay reassignment and an
    in-bay position perturbation; injected per worker profile so different
    multi-start trajectories emphasise different parts of the search space.
    """
    a = state.assignments[bi]
    blk = prob_info["blocks"][bi]
    fit = F.bay_fit.get((bi, a.orient_idx), [])
    if not fit:
        return None
    if random.random() < bay_change_prob and len(fit) > 1:
        alt = [b for b in fit if b != a.bay_id] or fit
        new_bid = random.choice(alt)
        old_bid = a.bay_id
        state.bay_blocks[old_bid].discard(bi)
        state.bay_blocks[new_bid].add(bi)
        state.bay_workload[old_bid] -= blk["workload"]
        state.bay_workload[new_bid] += blk["workload"]
        a.bay_id = new_bid
        anchor = F.safe_anchor.get((bi, a.orient_idx, new_bid))
        if anchor is not None:
            a.x, a.y = anchor
        else:
            bb = F.aabb.get((bi, a.orient_idx))
            if bb is not None:
                a.x = max(0, int(math.ceil(-bb[0])))
                a.y = max(0, int(math.ceil(-bb[1])))
        return "medium_bay_change"
    bay = bays[a.bay_id]
    bb = F.aabb.get((bi, a.orient_idx))
    if bb is None:
        return None
    bounds = F.anchor_bounds.get((bi, a.orient_idx, a.bay_id))
    if bounds is not None:
        min_x, max_x, min_y, max_y = bounds
    else:
        min_x = 0
        min_y = 0
        max_x = int(bay.width - (bb[2] - bb[0]))
        max_y = int(bay.height - (bb[3] - bb[1]))
    dx = random.choice([-3, -2, -1, 1, 2, 3])
    dy = random.choice([-2, -1, 1, 2])
    a.x = max(min_x, min(max_x, a.x + dx))
    a.y = max(min_y, min(max_y, a.y + dy))
    return "medium_pos_perturb"
