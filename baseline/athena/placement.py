"""Bay ranking, slot finding, and initial placement for Athena."""
from __future__ import annotations

import math
import time

from utils import Bay, Block, check_collisions, check_entry, check_exit

from .features import Features
from .state import _bay_weights, _compute_z2

# Phase 3 -- bay candidate ranking
# -----------------------------------------------------------------------------

def rank_bays_for_block(prob_info: dict, F: Features, bays: list[Bay],
                         bi: int, bay_loads: list[float],
                         w1: float, w2: float, w3: float,
                         bay_weights: list[float] | None = None
                         ) -> list[tuple[float, int, int]]:
    blk = prob_info["blocks"][bi]
    prefs = blk["bay_preferences"]
    s_max = max(prefs)
    wl = blk["workload"]
    out: list[tuple[float, int, int]] = []
    avg_load = sum(bay_loads) / max(1, len(bays))
    for oi in range(len(blk["shape"])):
        fit = F.bay_fit.get((bi, oi), [])
        for bid in fit:
            pref_pen = s_max - prefs[bid]
            if bay_weights is None:
                # legacy unweighted mean-deviation proxy (SA-repair path)
                imbalance = abs(bay_loads[bid] + wl - avg_load)
            else:
                # 1-A: marginal official obj2 (area-weighted max-pairwise)
                trial = bay_loads[:]
                trial[bid] += wl
                imbalance = _compute_z2(trial, bay_weights)
            area_room = (bays[bid].width * bays[bid].height) - F.area_top.get((bi, oi), 0.0)
            score = w3 * pref_pen + w2 * imbalance + 1e-4 * area_room
            out.append((score, bid, oi))
    out.sort()
    return out


# -----------------------------------------------------------------------------
# Phase 4 -- positional placement helpers
# -----------------------------------------------------------------------------

def _candidate_positions(bay: Bay, blk_aabb: tuple,
                          placed_in_bay: list[Block]) -> list[tuple[int, int]]:
    """Bottom-left fill candidates (same idea as baseline_greedy)."""
    lx0, ly0, lx1, ly1 = blk_aabb
    xs = {max(0, math.ceil(-lx0))}
    ys = {max(0, math.ceil(-ly0))}
    for b in placed_in_bay:
        bb = b.bounding_rect()
        xs.add(int(math.ceil(bb[2] - lx0)))
        ys.add(int(math.ceil(bb[3] - ly0)))
    out: list[tuple[int, int]] = []
    for x in sorted(xs):
        for y in sorted(ys):
            if x + lx1 <= bay.width + 1e-6 and y + ly1 <= bay.height + 1e-6:
                out.append((int(x), int(y)))
    return out


def _time_overlap(a1: int, e1: int, a2: int, e2: int) -> bool:
    return a1 < e2 and a2 < e1


def _find_earliest_slot(bay: Bay,
                         placed_in_bay: list[Block],
                         schedule_in_bay: list[tuple[int, int]],
                         new_blk: Block,
                         r_time: int,
                         proc: int,
                         deadline: float) -> tuple[int | None, int | None]:
    """Self-contained crane-feasible slot finder.

    Mirrors Stage-2/3/4 + future-exit checks of the Hermes custom slot
    finder but does not require any patching of utils.
    """
    cands = sorted({r_time} | {e for _, e in schedule_in_bay})
    for ec in cands:
        if deadline is not None and time.time() >= deadline:
            return None, None
        entry = max(r_time, ec)
        exit_t = entry + proc

        present_entry = [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a <= entry < e
        ]
        if check_entry(bay, present_entry, new_blk, fast=True):
            continue

        present_exit = [new_blk] + [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a < exit_t < e
        ]
        if check_exit(bay, present_exit, new_blk, fast=True):
            continue

        s4_blocked = False
        for b, (a, e) in zip(placed_in_bay, schedule_in_bay):
            if a <= entry or e >= exit_t:
                continue
            if not _time_overlap(entry, exit_t, a, e):
                continue
            if check_collisions(bay, [new_blk, b]):
                s4_blocked = True
                break
        if s4_blocked:
            continue

        future_exit_blocked = False
        for b, (a, e) in zip(placed_in_bay, schedule_in_bay):
            if entry < e < exit_t:
                if check_exit(bay, [new_blk], b, fast=True):
                    future_exit_blocked = True
                    break
        if future_exit_blocked:
            continue

        # Future ENTRY blocking: any already-placed block whose entry time
        # falls inside [entry, exit_t) will try to enter while new_blk is
        # present. new_blk must not obstruct that future entry. (Symmetric
        # of the future-exit check above; without it, Stage-2 collisions
        # leak into the final check_feasibility pass on dense instances.)
        future_entry_blocked = False
        for b, (a, e) in zip(placed_in_bay, schedule_in_bay):
            if entry <= a < exit_t:
                if check_entry(bay, [new_blk], b, fast=True):
                    future_entry_blocked = True
                    break
        if future_entry_blocked:
            continue

        return entry, exit_t
    return None, None


def _empty_bay_entry(schedule_in_bay: list[tuple[int, int]],
                      r_time: int, proc: int) -> int:
    entry = int(r_time)
    changed = True
    while changed:
        changed = False
        exit_t = entry + proc
        for a, e in schedule_in_bay:
            if _time_overlap(entry, exit_t, a, e):
                entry = max(entry, e)
                changed = True
    return entry


def _force_place(bi: int, prob_info: dict, F: Features, bays: list[Bay],
                  bay_schedule: list[list[tuple[int, int]]]) -> tuple:
    blk = prob_info["blocks"][bi]
    r = int(blk["release_time"])
    p = int(blk["processing_time"])
    prefs = blk["bay_preferences"]
    order = sorted(range(len(bays)), key=lambda j: prefs[j], reverse=True)
    for bid in order:
        bay = bays[bid]
        for oi in range(len(blk["shape"])):
            dims = F.dims.get((bi, oi))
            if dims is None:
                continue
            w, h = dims
            if w <= bay.width + 1e-6 and h <= bay.height + 1e-6:
                bb = F.aabb[(bi, oi)]
                px = max(0, math.ceil(-bb[0]))
                py = max(0, math.ceil(-bb[1]))
                e = _empty_bay_entry(bay_schedule[bid], r, p)
                return (bid, int(px), int(py), oi, int(e), int(e + p))
    # absolute last resort
    bid = order[0]
    bb = F.aabb.get((bi, 0), (0.0, 0.0, 1.0, 1.0))
    px = max(0, math.ceil(-bb[0]))
    py = max(0, math.ceil(-bb[1]))
    e = _empty_bay_entry(bay_schedule[bid], r, p)
    return (bid, int(px), int(py), 0, int(e), int(e + p))


def _placement_score(tard: float, blk_workload: float,
                     bay_loads: list[float], bid: int,
                     pref_pen: float, top_y: float,
                     w1: float, w2: float, w3: float,
                     bay_weights: list[float] | None = None) -> float:
    if bay_weights is None:
        # legacy unweighted mean-deviation proxy (SA-repair path)
        new_load = bay_loads[bid] + blk_workload
        n = len(bay_loads)
        avg = (sum(bay_loads) + blk_workload) / n
        max_dev = 0.0
        for j in range(n):
            cand = (new_load if j == bid else bay_loads[j])
            d = abs(cand - avg)
            if d > max_dev:
                max_dev = d
        imbalance = max_dev
    else:
        # 1-A: marginal official obj2 (area-weighted max-pairwise)
        trial = bay_loads[:]
        trial[bid] += blk_workload
        imbalance = _compute_z2(trial, bay_weights)
    return w1 * tard + w2 * imbalance + w3 * pref_pen + 1e-4 * top_y


def place_initial(prob_info: dict, F: Features, bays: list[Bay],
                  target_entry: list[int], target_orient: list[int],
                  w1: float, w2: float, w3: float, deadline: float,
                  bay_cands_cap: int = 4, pos_cands_cap: int = 12) -> tuple[dict, int]:
    blocks = prob_info["blocks"]
    n = len(blocks)
    # 1-A: construction uses the official area-weighted obj2 metric (see
    # _bay_weights / _compute_z2) so the initial solution optimises the same
    # imbalance the SA + final objective scores.
    bay_weights = _bay_weights(bays)

    def _area0(i: int) -> float:
        return F.area_top.get((i, target_orient[i]), 0.0)

    # Time-sorted sweep, ties broken by EDD and (largest area first to lock dense ones in).
    order = sorted(
        range(n),
        key=lambda i: (target_entry[i], blocks[i]["due_date"], -_area0(i)),
    )

    bay_placed: list[list[Block]] = [[] for _ in bays]
    bay_schedule: list[list[tuple[int, int]]] = [[] for _ in bays]
    bay_loads: list[float] = [0.0] * len(bays)
    assignments: dict = {}
    n_forced = 0

    for bi in order:
        blk = blocks[bi]
        r = int(blk["release_time"])
        p = int(blk["processing_time"])
        due = int(blk["due_date"])
        prefs = blk["bay_preferences"]
        s_max = max(prefs)
        tgt_e = max(r, target_entry[bi])

        best_score = float("inf")
        best: tuple | None = None

        if deadline is None or time.time() < deadline:
            ranked = rank_bays_for_block(prob_info, F, bays, bi, bay_loads,
                                          w1, w2, w3, bay_weights)
            # First pass: respect target_entry as soft lower bound
            for _, bid, oi in ranked[:bay_cands_cap]:
                if deadline is not None and time.time() >= deadline:
                    break
                bay = bays[bid]
                blk_bb = F.aabb[(bi, oi)]
                cands = _candidate_positions(bay, blk_bb, bay_placed[bid])[:pos_cands_cap]
                for cx, cy in cands:
                    new_blk = Block(block_id=bi, block_data=blk, x=cx, y=cy, orient_idx=oi)
                    if not bay.contains_block(new_blk):
                        continue
                    e, e_t = _find_earliest_slot(bay, bay_placed[bid], bay_schedule[bid],
                                                  new_blk, tgt_e, p, deadline)
                    if e is None:
                        continue
                    tard = max(0, e_t - due)
                    score = _placement_score(tard, blk["workload"], bay_loads, bid,
                                              s_max - prefs[bid], cy + blk_bb[3],
                                              w1, w2, w3, bay_weights)
                    if score < best_score:
                        best_score = score
                        best = (bid, cx, cy, oi, e, e_t)

            # Second pass: relax to release_time
            if best is None:
                for _, bid, oi in ranked[:bay_cands_cap]:
                    if deadline is not None and time.time() >= deadline:
                        break
                    bay = bays[bid]
                    blk_bb = F.aabb[(bi, oi)]
                    cands = _candidate_positions(bay, blk_bb, bay_placed[bid])[:pos_cands_cap]
                    for cx, cy in cands:
                        new_blk = Block(block_id=bi, block_data=blk, x=cx, y=cy, orient_idx=oi)
                        if not bay.contains_block(new_blk):
                            continue
                        e, e_t = _find_earliest_slot(bay, bay_placed[bid], bay_schedule[bid],
                                                      new_blk, r, p, deadline)
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
            best = _force_place(bi, prob_info, F, bays, bay_schedule)
            n_forced += 1

        bid, cx, cy, oi, e, e_t = best
        bay_placed[bid].append(Block(block_id=bi, block_data=blocks[bi], x=cx, y=cy, orient_idx=oi))
        bay_schedule[bid].append((e, e_t))
        bay_loads[bid] += blocks[bi]["workload"]
        assignments[bi] = {
            "block_id": bi,
            "bay_id": bid,
            "x": int(cx), "y": int(cy), "orient_idx": oi,
            "entry_time": int(e), "exit_time": int(e_t),
        }
    return assignments, n_forced
