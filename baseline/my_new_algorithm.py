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

import contextlib
import json
import math
import os
import random
import sys
import time

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
            if entry < a < exit_t:
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


def _apply_small_move(assignments: dict, prob_info: dict, F: Features) -> None:
    bi = random.choice(list(assignments.keys()))
    a = assignments[bi]
    blk = prob_info["blocks"][bi]
    if random.random() < 0.6:
        # entry shift
        delta = random.choice([-5, -3, -2, -1, 1, 2, 3, 5])
        new_e = max(int(blk["release_time"]), int(a["entry_time"]) + delta)
        a["entry_time"] = new_e
        a["exit_time"] = new_e + int(blk["processing_time"])
    else:
        n_or = len(blk["shape"])
        if n_or > 1:
            new_oi = random.randrange(n_or)
            a["orient_idx"] = new_oi
            bb = F.aabb.get((bi, new_oi))
            if bb is not None:
                # snap position so that the new orientation still fits at a sane corner
                a["x"] = max(0, int(math.ceil(-bb[0])))
                a["y"] = max(0, int(math.ceil(-bb[1])))


def _apply_medium_move(assignments: dict, prob_info: dict, F: Features,
                        bays: list[Bay]) -> None:
    bi = random.choice(list(assignments.keys()))
    a = assignments[bi]
    blk = prob_info["blocks"][bi]
    fit = F.bay_fit.get((bi, a["orient_idx"]), [])
    if not fit:
        return
    if random.random() < 0.5 and len(fit) > 1:
        new_bid = random.choice([b for b in fit if b != a["bay_id"]] or fit)
        a["bay_id"] = new_bid
        bb = F.aabb.get((bi, a["orient_idx"]))
        if bb is not None:
            a["x"] = max(0, int(math.ceil(-bb[0])))
            a["y"] = max(0, int(math.ceil(-bb[1])))
    else:
        # position perturbation
        bay = bays[a["bay_id"]]
        bb = F.aabb.get((bi, a["orient_idx"]))
        if bb is None:
            return
        max_x = int(bay.width - (bb[2] - bb[0]))
        max_y = int(bay.height - (bb[3] - bb[1]))
        dx = random.choice([-3, -2, -1, 1, 2, 3])
        dy = random.choice([-2, -1, 1, 2])
        a["x"] = max(0, min(max_x, a["x"] + dx))
        a["y"] = max(0, min(max_y, a["y"] + dy))


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


def sa_loop(prob_info: dict, F: Features, bays: list[Bay],
            w1: float, w2: float, w3: float,
            assignments: dict, deadline: float) -> tuple[dict, dict, float, int, int]:
    res, sol = evaluate_solution(prob_info, assignments)
    if res["feasible"]:
        best_obj = float(res["objective"])
        curr_obj = best_obj
    else:
        best_obj = float("inf")
        curr_obj = float("inf")
    best_sol = sol
    best_assign = {k: dict(v) for k, v in assignments.items()}

    T = 100.0
    cooling = 0.97
    iters = 0
    improvements = 0
    accepts = 0

    while time.time() < deadline:
        iters += 1
        snapshot = {k: dict(v) for k, v in assignments.items()}

        r = random.random()
        if r < 0.55:
            move_type = "small"
            _apply_small_move(assignments, prob_info, F)
        elif r < 0.88:
            move_type = "medium"
            _apply_medium_move(assignments, prob_info, F, bays)
        else:
            move_type = "large"
            _apply_large_move(assignments, prob_info, F, bays, deadline, w1, w2, w3)

        res, sol = evaluate_solution(prob_info, assignments)
        if res["feasible"]:
            obj = float(res["objective"])
        else:
            obj = float("inf")

        accept = False
        if obj < curr_obj:
            accept = True
        elif obj == float("inf"):
            accept = False
        else:
            d_obj = obj - curr_obj
            if random.random() < math.exp(-d_obj / max(1.0, T)):
                accept = True

        if accept:
            accepts += 1
            curr_obj = obj
            if obj < best_obj:
                best_obj = obj
                best_sol = sol
                best_assign = {k: dict(v) for k, v in assignments.items()}
                improvements += 1
                _emit("sa.improvement", iteration=iters, move_type=move_type, objective=obj)
        else:
            assignments.clear()
            assignments.update(snapshot)

        T *= cooling
        if T < 0.01:
            T = 100.0

    _emit("sa.complete", iterations=iters, improvements=improvements,
          accepts=accepts, best_objective=best_obj)
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

    # Phase 5
    sa_deadline = min(hard_deadline - safety, t_start + max(4.0, timelimit * 0.92))
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
