"""Bay ranking, slot finding, and initial placement for Athena."""
from __future__ import annotations

import math
import time

from utils import Bay, Block, check_collisions, check_entry, check_exit

from .features import Features
from .geometry import any_crane_obstructs_exact, crane_obstructs_exact, pair_collides_exact
from .state import _bay_weights, _compute_z2

_TEMPORAL_RANK_ALPHA = 0.05
_TEMPORAL_RANK_MIN_BLOCKS = 50

# Phase 3 -- bay candidate ranking
# -----------------------------------------------------------------------------

def rank_bays_for_block(prob_info: dict, F: Features, bays: list[Bay],
                         bi: int, bay_loads: list[float],
                         w1: float, w2: float, w3: float,
                         bay_weights: list[float] | None = None,
                         bay_schedule: list[list[tuple[int, int]]] | None = None,
                         earliest_entry: int | None = None,
                         proc: int | None = None,
                         due: int | None = None
                         ) -> list[tuple[float, int, int]]:
    blk = prob_info["blocks"][bi]
    prefs = blk["bay_preferences"]
    s_max = max(prefs)
    wl = blk["workload"]
    n_blocks = len(prob_info["blocks"])
    out: list[tuple[float, int, int]] = []
    avg_load = sum(bay_loads) / max(1, len(bays))
    temporal_cache: dict[int, float] = {}
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
            temporal_penalty = 0.0
            if (n_blocks >= _TEMPORAL_RANK_MIN_BLOCKS
                    and bay_schedule is not None and earliest_entry is not None
                    and proc is not None and due is not None):
                cached = temporal_cache.get(bid)
                if cached is None:
                    est_entry = _schedule_only_entry_proxy(
                        bay_schedule[bid], earliest_entry, proc)
                    cached = _TEMPORAL_RANK_ALPHA * w1 * max(0, est_entry + proc - due)
                    temporal_cache[bid] = cached
                temporal_penalty = cached
            area_room = (bays[bid].width * bays[bid].height) - F.area_top.get((bi, oi), 0.0)
            score = temporal_penalty + w3 * pref_pen + w2 * imbalance + 1e-4 * area_room
            out.append((score, bid, oi))
    out.sort()
    return out


def _cap_ranked_bay_candidates(ranked: list[tuple[float, int, int]],
                               cap: int) -> list[tuple[float, int, int]]:
    """Keep the candidate list compact while preserving distinct bay coverage."""
    if cap <= 0 or len(ranked) <= cap:
        return ranked
    selected: list[tuple[float, int, int]] = []
    seen_bays: set[int] = set()
    for cand in ranked:
        bid = cand[1]
        if bid in seen_bays:
            continue
        selected.append(cand)
        seen_bays.add(bid)
        if len(selected) >= cap:
            return selected
    selected_set = set(selected)
    for cand in ranked:
        if cand in selected_set:
            continue
        selected.append(cand)
        if len(selected) >= cap:
            break
    return selected


# -----------------------------------------------------------------------------
# Phase 4 -- positional placement helpers
# -----------------------------------------------------------------------------

def _anchor_bounds(bay: Bay, blk_aabb: tuple) -> tuple[int, int, int, int] | None:
    """Integer anchor bounds that keep every layer's AABB inside the bay."""
    lx0, ly0, lx1, ly1 = blk_aabb
    x_lo = int(math.ceil(-lx0 - 1e-9))
    y_lo = int(math.ceil(-ly0 - 1e-9))
    x_hi = int(math.floor(bay.width - lx1 + 1e-9))
    y_hi = int(math.floor(bay.height - ly1 + 1e-9))
    if x_lo > x_hi or y_lo > y_hi:
        return None
    return x_lo, x_hi, y_lo, y_hi


def _safe_anchor_position(bi: int, oi: int, prob_info: dict, F: Features,
                          bay: Bay) -> tuple[int, int] | None:
    """Return a verified bottom-left safe anchor, or None if no integer fit."""
    cached = F.safe_anchor.get((bi, oi, bay.id))
    if cached is not None:
        return cached
    bb = F.aabb.get((bi, oi))
    if bb is None:
        return None
    bounds = _anchor_bounds(bay, bb)
    if bounds is None:
        return None
    x_lo, x_hi, y_lo, y_hi = bounds
    # Prefer the lower-left anchor, but verify because Block is the final
    # authority for local-coordinate anchoring.
    for x, y in ((x_lo, y_lo), (x_hi, y_lo), (x_lo, y_hi), (x_hi, y_hi)):
        blk = Block(block_id=bi, block_data=prob_info["blocks"][bi],
                    x=int(x), y=int(y), orient_idx=oi)
        if bay.contains_block(blk):
            return int(x), int(y)
    return None


def _candidate_positions(bay: Bay, blk_aabb: tuple,
                          placed_in_bay: list[Block],
                          bounds: tuple[int, int, int, int] | None = None
                          ) -> list[tuple[int, int]]:
    """Bottom-left fill candidates (same idea as baseline_greedy)."""
    bounds = bounds if bounds is not None else _anchor_bounds(bay, blk_aabb)
    if bounds is None:
        return []
    x_lo, x_hi, y_lo, y_hi = bounds
    lx0, ly0, lx1, ly1 = blk_aabb
    xs = {x_lo}
    ys = {y_lo}
    for b in placed_in_bay:
        bb = b.bounding_rect()
        xs.add(int(math.ceil(bb[2] - lx0)))
        ys.add(int(math.ceil(bb[3] - ly0)))
    out: list[tuple[int, int]] = []
    for x in sorted(xs):
        for y in sorted(ys):
            if (x_lo <= x <= x_hi and y_lo <= y <= y_hi
                    and x + lx1 <= bay.width + 1e-6
                    and y + ly1 <= bay.height + 1e-6
                    and x + lx0 >= -1e-6
                    and y + ly0 >= -1e-6):
                out.append((int(x), int(y)))
    return out


def _time_overlap(a1: int, e1: int, a2: int, e2: int) -> bool:
    return a1 < e2 and a2 < e1


def _schedule_only_entry_proxy(schedule_in_bay: list[tuple[int, int]],
                               r_time: int, proc: int) -> int:
    """Cheap bay-timeline proxy for ranking before geometry is known."""
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


def _find_earliest_slot(bay: Bay,
                         placed_in_bay: list[Block],
                         schedule_in_bay: list[tuple[int, int]],
                         new_blk: Block,
                         r_time: int,
                         proc: int,
                         deadline: float,
                         F: Features | None = None) -> tuple[int | None, int | None]:
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

        # Stage 2 uses assignment-level presence (a <= entry < e), which is
        # stricter than same-time ENTRY replay ordering. Keep the stricter set
        # so candidates cannot pass Stage 5 while failing Stage 2.
        present_entry = [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a <= entry < e
        ]
        if F is None:
            entry_blocked = bool(check_entry(bay, present_entry, new_blk, fast=True))
        else:
            entry_blocked = any_crane_obstructs_exact(F, present_entry, new_blk)
        if entry_blocked:
            continue

        # Stage 3 ignores same-time exits, but Stage 5 replays EXIT ops by
        # block_id. A block with the same exit_t and a higher id is still
        # present when new_blk exits, so include it here to prevent replay
        # chains that only appear in Stage 5.
        present_exit = [new_blk] + [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if (a < exit_t < e)
            or (a < exit_t and e == exit_t and b.block_id > new_blk.block_id)
        ]
        if F is None:
            exit_blocked = bool(check_exit(bay, present_exit, new_blk, fast=True))
        else:
            exit_blocked = any_crane_obstructs_exact(F, present_exit, new_blk)
        if exit_blocked:
            continue

        s4_blocked = False
        for b, (a, e) in zip(placed_in_bay, schedule_in_bay):
            if not _time_overlap(entry, exit_t, a, e):
                continue
            if (pair_collides_exact(F, new_blk, b)
                    if F is not None else check_collisions(bay, [new_blk, b])):
                s4_blocked = True
                break
        if s4_blocked:
            continue

        future_exit_blocked = False
        for b, (a, e) in zip(placed_in_bay, schedule_in_bay):
            new_present_for_exit = (
                entry < e < exit_t
                or (entry < e and e == exit_t and new_blk.block_id > b.block_id)
            )
            if new_present_for_exit:
                if (crane_obstructs_exact(F, new_blk, b)
                        if F is not None
                        else check_exit(bay, [new_blk, b], b, fast=True)):
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
            # Stage 2 considers same-time entries co-present, so use the
            # conservative condition even though Stage 5 orders ENTRY by id.
            if entry <= a < exit_t:
                if (crane_obstructs_exact(F, new_blk, b)
                        if F is not None
                        else check_entry(bay, [new_blk], b, fast=True)):
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


def _safe_serial_place_after_all(bi: int, prob_info: dict, F: Features,
                                 bays: list[Bay],
                                 bay_schedule: list[list[tuple[int, int]]],
                                 bay_order: list[int] | None = None
                                 ) -> tuple | None:
    """Last-resort safe placement: choose a fitting bay/orientation and place
    after every existing interval in that bay. This may be very tardy, but it
    never fabricates a boundary-invalid assignment.
    """
    blk = prob_info["blocks"][bi]
    r = int(blk["release_time"])
    p = int(blk["processing_time"])
    prefs = blk["bay_preferences"]
    order = bay_order or sorted(range(len(bays)), key=lambda j: prefs[j], reverse=True)
    best_key: tuple | None = None
    best: tuple | None = None
    for order_idx, bid in enumerate(order):
        bay = bays[bid]
        for oi in range(len(blk["shape"])):
            pos = _safe_anchor_position(bi, oi, prob_info, F, bay)
            if pos is None:
                continue
            x, y = pos
            e = max(r, max((end for _, end in bay_schedule[bid]), default=r))
            e = _empty_bay_entry(bay_schedule[bid], e, p)
            exit_t = e + p
            key = (
                max(0, exit_t - int(blk["due_date"])),
                exit_t,
                order_idx,
                -prefs[bid],
                oi,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = (bid, int(x), int(y), oi, int(e), int(exit_t))
    return best


def _safe_fallback_place(bi: int, prob_info: dict, F: Features, bays: list[Bay],
                         bay_placed: list[list[Block]],
                         bay_schedule: list[list[tuple[int, int]]],
                         bay_loads: list[float],
                         w1: float, w2: float, w3: float,
                         bay_weights: list[float] | None,
                         deadline: float | None,
                         earliest_entry: int | None = None,
                         pos_cands_cap: int = 64) -> tuple | None:
    """Safe replacement for the old force-place path.

    It first tries a broad replay-aware best-fit search. If that is exhausted
    or the deadline is already tight, it falls back to a serial after-all slot
    that preserves feasibility at the cost of tardiness.
    """
    blk = prob_info["blocks"][bi]
    r = int(blk["release_time"])
    p = int(blk["processing_time"])
    due = int(blk["due_date"])
    prefs = blk["bay_preferences"]
    s_max = max(prefs)
    start = max(r, int(earliest_entry if earliest_entry is not None else r))
    ranked = rank_bays_for_block(prob_info, F, bays, bi, bay_loads,
                                  w1, w2, w3, bay_weights,
                                  bay_schedule, start, p, due)
    if not ranked:
        bay_order = sorted(range(len(bays)), key=lambda j: prefs[j], reverse=True)
        return _safe_serial_place_after_all(bi, prob_info, F, bays,
                                            bay_schedule, bay_order)

    best_score = float("inf")
    best: tuple | None = None
    for _, bid, oi in ranked:
        if deadline is not None and time.time() >= deadline:
            break
        bay = bays[bid]
        blk_bb = F.aabb.get((bi, oi))
        if blk_bb is None:
            continue
        cands = _candidate_positions(
            bay, blk_bb, bay_placed[bid],
            F.anchor_bounds.get((bi, oi, bid)))
        safe_pos = _safe_anchor_position(bi, oi, prob_info, F, bay)
        if safe_pos is not None and safe_pos not in cands:
            cands.insert(0, safe_pos)
        for cx, cy in cands[:pos_cands_cap]:
            new_blk = Block(block_id=bi, block_data=blk, x=cx, y=cy, orient_idx=oi)
            if not bay.contains_block(new_blk):
                continue
            e, e_t = _find_earliest_slot(bay, bay_placed[bid], bay_schedule[bid],
                                          new_blk, start, p, deadline, F)
            if e is None:
                continue
            tard = max(0, e_t - due)
            score = _placement_score(tard, blk["workload"], bay_loads, bid,
                                      s_max - prefs[bid], cy + blk_bb[3],
                                      w1, w2, w3, bay_weights)
            if score < best_score:
                best_score = score
                best = (bid, cx, cy, oi, e, e_t)
    if best is not None:
        return best

    bay_order = [bid for _, bid, _ in ranked]
    seen = set()
    bay_order = [b for b in bay_order if not (b in seen or seen.add(b))]
    return _safe_serial_place_after_all(bi, prob_info, F, bays,
                                        bay_schedule, bay_order)


# Backward-compatible name for older imports. It is intentionally safe now.
def _force_place(bi: int, prob_info: dict, F: Features, bays: list[Bay],
                  bay_schedule: list[list[tuple[int, int]]]) -> tuple | None:
    return _safe_serial_place_after_all(bi, prob_info, F, bays, bay_schedule)


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
                  bay_cands_cap: int = 4, pos_cands_cap: int = 12
                  ) -> tuple[dict, int, int]:
    blocks = prob_info["blocks"]
    n = len(blocks)
    # 1-A: construction uses the official area-weighted obj2 metric (see
    # _bay_weights / _compute_z2) so the initial solution optimises the same
    # imbalance the SA + final objective scores.
    bay_weights = _bay_weights(bays)
    # 1-C: do not prune away an entire bay on small/medium bay-count suites.
    # B5 instances should try all 5 bays even when the historical default cap is 4.
    effective_bay_cands_cap = max(bay_cands_cap, len(bays))

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
    n_unplaced = 0

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
                                          w1, w2, w3, bay_weights,
                                          bay_schedule, tgt_e, p, due)
            # First pass: respect target_entry as soft lower bound
            capped_ranked = _cap_ranked_bay_candidates(ranked, effective_bay_cands_cap)
            for _, bid, oi in capped_ranked:
                if deadline is not None and time.time() >= deadline:
                    break
                bay = bays[bid]
                blk_bb = F.aabb[(bi, oi)]
                cands = _candidate_positions(
                    bay, blk_bb, bay_placed[bid],
                    F.anchor_bounds.get((bi, oi, bid)))[:pos_cands_cap]
                for cx, cy in cands:
                    new_blk = Block(block_id=bi, block_data=blk, x=cx, y=cy, orient_idx=oi)
                    if not bay.contains_block(new_blk):
                        continue
                    e, e_t = _find_earliest_slot(bay, bay_placed[bid], bay_schedule[bid],
                                                  new_blk, tgt_e, p, deadline, F)
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
                for _, bid, oi in capped_ranked:
                    if deadline is not None and time.time() >= deadline:
                        break
                    bay = bays[bid]
                    blk_bb = F.aabb[(bi, oi)]
                    cands = _candidate_positions(
                        bay, blk_bb, bay_placed[bid],
                        F.anchor_bounds.get((bi, oi, bid)))[:pos_cands_cap]
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
                deadline=deadline,
                earliest_entry=r,
                pos_cands_cap=max(pos_cands_cap * 4, 48),
            )
            if best is not None:
                n_forced += 1

        if best is None:
            n_unplaced += 1
            continue

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
    return assignments, n_forced, n_unplaced
