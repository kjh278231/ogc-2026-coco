"""
my_new_algorithm.py -- "Athena" Solver

A completely new (non-Hermes) algorithm exploring an alternative pipeline:

  1. Per-(block, orient) feature precomputation
       AABB, OBB, per-layer area, max_layer_count, bottom-footprint area,
       crane-risk heuristic, bay-fit candidate list.

  2. Global time-window smoothing
       Per-block target_entry chosen by greedy load-flattening over an
       integer-time horizon. Cost weights peak load, variance contribution,
       and tardiness risk.

  3. Bay assignment scoring (kept weakly coupled with smoothing)
       For each block, rank (orient, bay) candidates by:
         preference_penalty + workload_imbalance_delta + area_fit + cong_delta

  4. Sweep-based positional placement
       Blocks sorted by (target_entry, due_date, -area). For each block, try
       ranked (bay, orient, bottom-left-corner positions). Crane-feasibility
       respected via a self-contained _find_earliest_slot that mirrors the
       Stage-2/3/4 + future-exit-blocking checks. Force-place fallback uses
       an empty-bay window for guaranteed feasibility.

  5. Hierarchical Simulated Annealing
       Small  : entry shift, orientation swap
       Medium : bay change, position perturbation
       Large  : tardy / overloaded-bay destroy + sweep-repair
       Acceptance: Metropolis with reheat. Each candidate is verified via
       utils.check_feasibility; infeasible candidates are rejected.

Public API:  algorithm(prob_info, timelimit) -> solution dict
Does NOT depend on baseline_greedy.py and does NOT modify utils.py.
Compatible with the contestant contract.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import math
import multiprocessing
import os
import random
import sys
import time
from dataclasses import dataclass, field

from shapely.geometry import Polygon
from shapely.affinity import translate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import utils  # noqa: E402
from utils import (  # noqa: E402
    Bay, Block,
    check_entry, check_exit, check_collisions, check_feasibility,
    _resolve_layers, _bounding_box, _poly_from_verts,
)


# -----------------------------------------------------------------------------
# Optional structured event log (mirrors the Hermes convention so existing
# eval tooling can pick traces up unchanged when OGC2026_EVENT_LOG is set).
# -----------------------------------------------------------------------------

_event_log_fh = None
_event_log_t0 = None


def _init_event_log(t0: float) -> None:
    global _event_log_fh, _event_log_t0
    _event_log_t0 = t0
    path = os.environ.get("OGC2026_EVENT_LOG")
    if not path:
        _event_log_fh = None
        return
    try:
        _event_log_fh = open(path, "a", buffering=1, encoding="utf-8")
    except Exception:
        _event_log_fh = None


def _close_event_log() -> None:
    global _event_log_fh
    if _event_log_fh is not None:
        try:
            _event_log_fh.close()
        except Exception:
            pass
        _event_log_fh = None


def _emit(event: str, **payload) -> None:
    if _event_log_fh is None:
        return
    try:
        rec = {"t": round(time.time() - _event_log_t0, 4), "event": event}
        rec.update(payload)
        _event_log_fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Phase 1 -- feature precomputation
# -----------------------------------------------------------------------------

class Features:
    __slots__ = (
        "aabb", "obb_local", "local_polys",
        "n_layers", "area_top", "area_sum", "crane_risk",
        "dims", "bay_fit",
    )

    def __init__(self) -> None:
        self.aabb: dict = {}          # (bi, oi) -> (minx, miny, maxx, maxy)
        self.obb_local: dict = {}     # (bi, oi) -> Shapely Polygon (local)
        self.local_polys: dict = {}   # (bi, oi) -> list[Polygon|None]
        self.n_layers: dict = {}      # (bi, oi) -> int
        self.area_top: dict = {}      # (bi, oi) -> float (layer 0 area)
        self.area_sum: dict = {}      # (bi, oi) -> float (sum of per-layer areas)
        self.crane_risk: dict = {}    # (bi, oi) -> float
        self.dims: dict = {}          # (bi, oi) -> (width, height)
        self.bay_fit: dict = {}       # (bi, oi) -> list[bay_id]


def precompute_features(prob_info: dict, bays: list[Bay]) -> Features:
    F = Features()
    for bi, blk in enumerate(prob_info["blocks"]):
        for oi, orient in enumerate(blk["shape"]):
            raw_layers = orient.get("layers", [])
            layers = _resolve_layers(raw_layers)
            if not layers:
                continue
            ref_x, ref_y = layers[0][0]
            shifted = [
                [[v[0] - ref_x, v[1] - ref_y] for v in l]
                for l in layers
            ]
            all_v = [v for l in shifted for v in l]
            bb = _bounding_box(all_v)
            F.aabb[(bi, oi)] = bb
            F.dims[(bi, oi)] = (bb[2] - bb[0], bb[3] - bb[1])

            polys = [_poly_from_verts(layer) for layer in shifted]
            F.local_polys[(bi, oi)] = polys

            areas = [(p.area if p is not None else 0.0) for p in polys]
            F.area_top[(bi, oi)] = areas[0] if areas else 0.0
            F.area_sum[(bi, oi)] = sum(areas)
            F.n_layers[(bi, oi)] = len(layers)
            F.crane_risk[(bi, oi)] = len(layers) * (areas[0] if areas else 1.0)

            try:
                F.obb_local[(bi, oi)] = Polygon(all_v).minimum_rotated_rectangle
            except Exception:
                F.obb_local[(bi, oi)] = None

            fit = []
            w, h = F.dims[(bi, oi)]
            for bid, bay in enumerate(bays):
                if w <= bay.width + 1e-6 and h <= bay.height + 1e-6:
                    fit.append(bid)
            F.bay_fit[(bi, oi)] = fit
    return F


# -----------------------------------------------------------------------------
# Phase 2 -- global time-window smoothing
# -----------------------------------------------------------------------------

def smooth_time_windows(prob_info: dict, F: Features,
                        max_cands_per_block: int = 60,
                        alpha_peak: float = 0.4,
                        beta_var: float = 1e-3,
                        gamma_tard: float = 4.0) -> tuple[list[int], list[int]]:
    """Return (target_entry, target_orient) lists indexed by block_id.

    Block processing order is least-slack-first. For each block, candidate
    entry times in [release, due - proc] are sampled (capped at
    max_cands_per_block) plus a few tardy options. Cost combines added peak
    load, variance contribution, and tardiness risk.
    """
    blocks = prob_info["blocks"]
    n = len(blocks)
    if n == 0:
        return [], []

    horizon = max(b["due_date"] + b["processing_time"] for b in blocks) + 8
    load = [0.0] * (horizon + 1)

    target_entry = [0] * n
    target_orient = [0] * n

    order = sorted(
        range(n),
        key=lambda i: (
            blocks[i]["due_date"] - blocks[i]["release_time"] - blocks[i]["processing_time"],
            blocks[i]["due_date"],
        ),
    )

    for bi in order:
        b = blocks[bi]
        r = int(b["release_time"])
        d = int(b["due_date"])
        p = int(b["processing_time"])
        w = float(b["workload"])

        lo = r
        hi = max(r, d - p)
        n_cands = hi - lo + 1
        if n_cands > max_cands_per_block:
            step = max(1, n_cands // max_cands_per_block)
            cands = list(range(lo, hi + 1, step))
            if cands[-1] != hi:
                cands.append(hi)
        else:
            cands = list(range(lo, hi + 1))
        # also add a few "tardy-but-cheap" candidates in case feasible window is tight
        for delta in (1, 3, 7):
            cands.append(hi + delta)

        best_cost = float("inf")
        best_e = lo
        for e in cands:
            if e < r:
                continue
            tard = max(0, e + p - d)
            peak = 0.0
            var_inc = 0.0
            t1 = e + p
            for t in range(e, t1):
                if t >= len(load):
                    continue
                old = load[t]
                new = old + w
                if new > peak:
                    peak = new
                var_inc += new * new - old * old
            cost = alpha_peak * peak + beta_var * var_inc + gamma_tard * tard
            if cost < best_cost:
                best_cost = cost
                best_e = e

        target_entry[bi] = best_e
        # pick the most square-ish orientation as the default initial guess
        best_oi = 0
        best_ratio = float("inf")
        for oi in range(len(b["shape"])):
            dims = F.dims.get((bi, oi))
            if dims is None:
                continue
            w_d, h_d = dims
            ratio = max(w_d, h_d) / max(1.0, min(w_d, h_d))
            if ratio < best_ratio:
                best_ratio = ratio
                best_oi = oi
        target_orient[bi] = best_oi

        for t in range(best_e, best_e + p):
            if 0 <= t < len(load):
                load[t] += w

    return target_entry, target_orient


# -----------------------------------------------------------------------------
# Phase 3 -- bay candidate ranking
# -----------------------------------------------------------------------------

def rank_bays_for_block(prob_info: dict, F: Features, bays: list[Bay],
                         bi: int, bay_loads: list[float],
                         w1: float, w2: float, w3: float) -> list[tuple[float, int, int]]:
    blk = prob_info["blocks"][bi]
    prefs = blk["bay_preferences"]
    s_max = max(prefs)
    out: list[tuple[float, int, int]] = []
    avg_load = sum(bay_loads) / max(1, len(bays))
    for oi in range(len(blk["shape"])):
        fit = F.bay_fit.get((bi, oi), [])
        for bid in fit:
            pref_pen = s_max - prefs[bid]
            new_load = bay_loads[bid] + blk["workload"]
            load_dev = abs(new_load - avg_load)
            area_room = (bays[bid].width * bays[bid].height) - F.area_top.get((bi, oi), 0.0)
            score = w3 * pref_pen + w2 * load_dev + 1e-4 * area_room
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
                     w1: float, w2: float, w3: float) -> float:
    new_load = bay_loads[bid] + blk_workload
    n = len(bay_loads)
    avg = (sum(bay_loads) + blk_workload) / n
    # workload-imbalance proxy: max deviation
    max_dev = 0.0
    for j in range(n):
        cand = (new_load if j == bid else bay_loads[j])
        d = abs(cand - avg)
        if d > max_dev:
            max_dev = d
    return w1 * tard + w2 * max_dev + w3 * pref_pen + 1e-4 * top_y


def place_initial(prob_info: dict, F: Features, bays: list[Bay],
                  target_entry: list[int], target_orient: list[int],
                  w1: float, w2: float, w3: float, deadline: float,
                  bay_cands_cap: int = 4, pos_cands_cap: int = 12) -> tuple[dict, int]:
    blocks = prob_info["blocks"]
    n = len(blocks)

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
            ranked = rank_bays_for_block(prob_info, F, bays, bi, bay_loads, w1, w2, w3)
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
                                              s_max - prefs[bid], cy + blk_bb[3], w1, w2, w3)
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
                                                  s_max - prefs[bid], cy + blk_bb[3], w1, w2, w3)
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


# -----------------------------------------------------------------------------
# Operations dict builder (EXIT precedes ENTRY within a time-point)
# -----------------------------------------------------------------------------

def assignments_to_solution(assignments: dict) -> dict:
    buckets: dict[int, list[tuple]] = {}
    for a in assignments.values():
        t_entry = int(a["entry_time"])
        t_exit = int(a["exit_time"])
        bid = a["block_id"]
        bay = a["bay_id"]
        buckets.setdefault(t_exit,  []).append((0, "EXIT",  bid, bay, None, None, None))
        buckets.setdefault(t_entry, []).append((1, "ENTRY", bid, bay, a["x"], a["y"], a["orient_idx"]))
    operations: dict[str, list[dict]] = {}
    for t in sorted(buckets):
        ops = sorted(buckets[t], key=lambda x: (x[0], x[2]))
        result = []
        for _, kind, bid, bay, x, y, orient_idx in ops:
            op: dict = {"type": kind, "block_id": bid, "bay_id": bay}
            if kind == "ENTRY":
                op["x"] = x
                op["y"] = y
                op["orient_idx"] = orient_idx
            result.append(op)
        operations[str(t)] = result
    return {"operations": operations}


def evaluate_solution(prob_info: dict, assignments: dict) -> tuple[dict, dict]:
    sol = assignments_to_solution(assignments)
    res = check_feasibility(prob_info, sol)
    return res, sol


# -----------------------------------------------------------------------------
# Phase 5 -- hierarchical SA
# -----------------------------------------------------------------------------

def _bay_workload_dict(assignments: dict, prob_info: dict, n_bays: int) -> list[float]:
    loads = [0.0] * n_bays
    for a in assignments.values():
        loads[a["bay_id"]] += prob_info["blocks"][a["block_id"]]["workload"]
    return loads


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
        bb = F.aabb.get((bi, a.orient_idx))
        if bb is not None:
            a.x = max(0, int(math.ceil(-bb[0])))
            a.y = max(0, int(math.ceil(-bb[1])))
    else:
        bay = bays[a.bay_id]
        bb = F.aabb.get((bi, a.orient_idx))
        if bb is None:
            return {bi}
        max_x = int(bay.width - (bb[2] - bb[0]))
        max_y = int(bay.height - (bb[3] - bb[1]))
        dx = random.choice([-3, -2, -1, 1, 2, 3])
        dy = random.choice([-2, -1, 1, 2])
        a.x = max(0, min(max_x, a.x + dx))
        a.y = max(0, min(max_y, a.y + dy))
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
        if a["exit_time"] > blocks[bi]["due_date"]:
            tardy.append(bi)
    if not tardy:
        k = max(1, n // 15)
        tardy = random.sample(list(assignments.keys()), min(k, n))
    # destroy a subset
    destroy_size = max(1, min(len(tardy), n // 10))
    destroyed = set(random.sample(tardy, destroy_size))

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

    # reinsert destroyed blocks in EDD order (one-shot best-fit)
    repair_order = sorted(destroyed, key=lambda bi: blocks[bi]["due_date"])
    for bi in repair_order:
        if deadline is not None and time.time() >= deadline:
            break
        blk = blocks[bi]
        r = int(blk["release_time"])
        p = int(blk["processing_time"])
        due = int(blk["due_date"])
        prefs = blk["bay_preferences"]
        s_max = max(prefs)
        ranked = rank_bays_for_block(prob_info, F, bays, bi, bay_loads, w1, w2, w3)
        best_score = float("inf")
        best = None
        for _, bid, oi in ranked[:3]:
            bay = bays[bid]
            blk_bb = F.aabb[(bi, oi)]
            cands = _candidate_positions(bay, blk_bb, bay_placed[bid])[:8]
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
                                          s_max - prefs[bid], cy + blk_bb[3], w1, w2, w3)
                if score < best_score:
                    best_score = score
                    best = (bid, cx, cy, oi, e, e_t)
        if best is None:
            best = _force_place(bi, prob_info, F, bays, bay_schedule)
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
        bb = F.aabb.get((bi, a.orient_idx))
        if bb is not None:
            a.x = max(0, int(math.ceil(-bb[0])))
            a.y = max(0, int(math.ceil(-bb[1])))
        return "medium_bay_change"
    bay = bays[a.bay_id]
    bb = F.aabb.get((bi, a.orient_idx))
    if bb is None:
        return None
    max_x = int(bay.width - (bb[2] - bb[0]))
    max_y = int(bay.height - (bb[3] - bb[1]))
    dx = random.choice([-3, -2, -1, 1, 2, 3])
    dy = random.choice([-2, -1, 1, 2])
    a.x = max(0, min(max_x, a.x + dx))
    a.y = max(0, min(max_y, a.y + dy))
    return "medium_pos_perturb"


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


# -----------------------------------------------------------------------------
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
