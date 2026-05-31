"""Incremental feasibility filters for Athena SA moves."""
from __future__ import annotations

import random

from utils import Bay, Block, check_collisions, check_entry, check_exit

from .features import Features
from .state import Assignment, FastState

# -----------------------------------------------------------------------------
# Same-time ENTRY/EXIT presence helpers
# -----------------------------------------------------------------------------
# Official check_feasibility groups ops at a single integer timepoint as
#   EXIT first (sorted by block_id ASC), then ENTRY (sorted by block_id ASC).
# These helpers mirror that exact ordering so the fast checker doesn't
# disagree with the official one at boundary moments.

def _is_present_at_entry_of(other: Assignment, target_t: int,
                              target_id: int) -> bool:
    """Is `other` block in the bay at the moment `target_id` ENTRYs at target_t?

    Present when:
      - other strictly contains target_t (interior); OR
      - other ENTRYs at the same t with strictly lower block_id (it executes
        first because ENTRY ops at the same t are sorted by id ASC).

    Note: other ENTRYing at target_t with HIGHER id has NOT yet entered when
    target enters. Other EXITing at target_t has already left (EXIT precedes
    ENTRY at the same t).
    """
    if other.entry_time < target_t < other.exit_time:
        return True
    if other.entry_time == target_t and other.block_id < target_id:
        return True
    return False


def _is_present_at_exit_of(other: Assignment, target_t: int,
                             target_id: int) -> bool:
    """Is `other` block in the bay at the moment `target_id` EXITs at target_t?

    Present when:
      - other strictly contains target_t (interior); OR
      - other EXITs at the same t with strictly higher block_id (it executes
        LATER in the EXIT-by-id-ASC ordering, so it is still present when
        target exits).

    Other ENTRYing at target_t has NOT yet entered (ENTRY phase is after
    EXIT phase). Other EXITing at target_t with lower id has already left.
    """
    if other.entry_time < target_t < other.exit_time:
        return True
    if (other.exit_time == target_t
            and other.entry_time < target_t
            and other.block_id > target_id):
        return True
    return False


# --------- Pair / crane caches with bounded size --------------------------

_PAIR_CACHE_CAP = 200_000
_CRANE_CACHE_CAP = 200_000


def _coll_key(a: Assignment, b: Assignment) -> tuple:
    ka = (a.block_id, a.orient_idx, a.x, a.y)
    kb = (b.block_id, b.orient_idx, b.x, b.y)
    if ka > kb:
        ka, kb = kb, ka
    return ka + kb


def _crane_key(existing: Assignment, new: Assignment, kind: str,
               bay_id: int) -> tuple:
    """Asymmetric: does `existing` obstruct `new`'s ENTRY/EXIT in bay `bay_id`?"""
    return (kind, bay_id,
            existing.block_id, existing.orient_idx, existing.x, existing.y,
            new.block_id, new.orient_idx, new.x, new.y)


def _cap_cache(cache: dict, cap: int) -> None:
    if len(cache) > cap:
        keys = list(cache.keys())
        random.shuffle(keys)
        for k in keys[: len(keys) // 5]:
            del cache[k]


def _pair_collides(a: Assignment, b: Assignment, prob_info: dict,
                    cache: dict) -> bool:
    key = _coll_key(a, b)
    if key in cache:
        return cache[key]
    blk_a = Block(block_id=a.block_id,
                  block_data=prob_info["blocks"][a.block_id],
                  x=a.x, y=a.y, orient_idx=a.orient_idx)
    blk_b = Block(block_id=b.block_id,
                  block_data=prob_info["blocks"][b.block_id],
                  x=b.x, y=b.y, orient_idx=b.orient_idx)
    # large dummy bay so boundary check passes; pairwise spatial overlap
    # is bay-independent.
    dummy = Bay(width=10_000, height=10_000, id=0)
    result = bool(check_collisions(dummy, [blk_a, blk_b]))
    cache[key] = result
    _cap_cache(cache, _PAIR_CACHE_CAP)
    return result


def _crane_obstructs(existing: Assignment, new: Assignment, kind: str,
                      prob_info: dict, bays: list[Bay], cache: dict) -> bool:
    """Does `existing` obstruct `new`'s ENTRY (kind='entry') or EXIT ('exit')?"""
    key = _crane_key(existing, new, kind, new.bay_id)
    if key in cache:
        return cache[key]
    bay = bays[new.bay_id]
    blk_e = Block(block_id=existing.block_id,
                  block_data=prob_info["blocks"][existing.block_id],
                  x=existing.x, y=existing.y, orient_idx=existing.orient_idx)
    blk_n = Block(block_id=new.block_id,
                  block_data=prob_info["blocks"][new.block_id],
                  x=new.x, y=new.y, orient_idx=new.orient_idx)
    if kind == "entry":
        result = bool(check_entry(bay, [blk_e], blk_n, fast=True))
    else:
        result = bool(check_exit(bay, [blk_e], blk_n, fast=True))
    cache[key] = result
    _cap_cache(cache, _CRANE_CACHE_CAP)
    return result


def fast_check_move(prob_info: dict, F: Features, bays: list[Bay],
                     state: FastState, snapshot: dict, changed_ids: set,
                     cache_pair: dict, cache_crane: dict) -> bool:
    """Quick partial feasibility filter for the small/medium move set.

    Verifies only what the move could have invalidated:
      1. per-block release/processing/bay-boundary
      2. spatial collision vs same-bay blocks with overlapping time window
      3. crane entry/exit of the changed block against co-present blocks
      4. crane entry/exit of co-present blocks against the *changed block*
         (i.e. the changed block must not start obstructing somebody else)

    Returns True if the move *seems* feasible. The official checker is the
    final authority for best-objective accepts (handled by caller).
    """
    blocks = prob_info["blocks"]

    # 1. per-block sanity
    for bi in changed_ids:
        a = state.assignments[bi]
        blk = blocks[bi]
        if a.entry_time < int(blk["release_time"]):
            return False
        if a.exit_time != a.entry_time + int(blk["processing_time"]):
            return False
        bay = bays[a.bay_id]
        b_obj = Block(block_id=bi, block_data=blk,
                      x=a.x, y=a.y, orient_idx=a.orient_idx)
        if not bay.contains_block(b_obj):
            return False

    # 2-4. against neighbors (in changed block's CURRENT bay)
    for bi in changed_ids:
        a = state.assignments[bi]
        neighbors = state.bay_blocks[a.bay_id] - {bi}
        # Also include changed peers that share the same bay (their movement
        # could collide with bi). For small/medium moves changed_ids is size 1,
        # so this is usually empty.
        for bk in neighbors:
            k = state.assignments[bk]

            time_overlap = (a.entry_time < k.exit_time) and (k.entry_time < a.exit_time)
            if not time_overlap:
                continue

            # (2) spatial overlap during their joint interval
            if _pair_collides(a, k, prob_info, cache_pair):
                return False

            # (3a) crane entry for the changed block:
            #      is k present at a.entry per the official ordering?
            if _is_present_at_entry_of(k, a.entry_time, a.block_id):
                if _crane_obstructs(k, a, "entry", prob_info, bays, cache_crane):
                    return False
            # (3b) crane exit for the changed block:
            #      is k present at a.exit per the official ordering?
            if _is_present_at_exit_of(k, a.exit_time, a.block_id):
                if _crane_obstructs(k, a, "exit", prob_info, bays, cache_crane):
                    return False
            # (4a) does the changed block obstruct k's ENTRY?
            #      a is "other" relative to k; check by k's perspective.
            if _is_present_at_entry_of(a, k.entry_time, k.block_id):
                if _crane_obstructs(a, k, "entry", prob_info, bays, cache_crane):
                    return False
            # (4b) does the changed block obstruct k's EXIT?
            if _is_present_at_exit_of(a, k.exit_time, k.block_id):
                if _crane_obstructs(a, k, "exit", prob_info, bays, cache_crane):
                    return False

    # Also: if the changed block's OLD bay differs from the new bay, the
    # neighbors in the *old* bay can't be invalidated by *removal* alone
    # (removal only un-blocks). So no extra check needed there.
    return True
