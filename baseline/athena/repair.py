"""Feasibility-first repair helpers for Athena construction."""
from __future__ import annotations

import re
import time

from utils import Bay, Block

from .events import _emit
from .features import Features
from .placement import _safe_fallback_place, _safe_serial_place_after_all
from .solution import evaluate_solution
from .state import _bay_weights

_BLOCK_RE = re.compile(r"block\s+(\d+)")
_TIME_RE = re.compile(r"t=(\d+)")


def _copy_assignments(assignments: dict) -> dict:
    return {int(k): dict(v) for k, v in assignments.items()}


def _rebuild_bay_state(assignments: dict, prob_info: dict,
                       bays: list[Bay]) -> tuple[list[list[Block]],
                                                 list[list[tuple[int, int]]],
                                                 list[float]]:
    blocks = prob_info["blocks"]
    bay_placed: list[list[Block]] = [[] for _ in bays]
    bay_schedule: list[list[tuple[int, int]]] = [[] for _ in bays]
    bay_loads: list[float] = [0.0] * len(bays)
    for bi in sorted(assignments, key=lambda i: assignments[i]["entry_time"]):
        a = assignments[bi]
        bid = int(a["bay_id"])
        bay_placed[bid].append(Block(
            block_id=bi, block_data=blocks[bi],
            x=int(a["x"]), y=int(a["y"]), orient_idx=int(a["orient_idx"]),
        ))
        bay_schedule[bid].append((int(a["entry_time"]), int(a["exit_time"])))
        bay_loads[bid] += blocks[bi]["workload"]
    return bay_placed, bay_schedule, bay_loads


def _area_risk(F: Features, bi: int) -> float:
    vals = [v for (i, _), v in F.area_sum.items() if i == bi]
    return max(vals) if vals else 0.0


def _crane_risk(F: Features, bi: int) -> float:
    vals = [v for (i, _), v in F.crane_risk.items() if i == bi]
    return max(vals) if vals else 0.0


def _repair_order_key(prob_info: dict, F: Features, bi: int) -> tuple:
    blk = prob_info["blocks"][bi]
    slack = (int(blk["due_date"]) - int(blk["release_time"])
             - int(blk["processing_time"]))
    return (
        slack,
        int(blk["due_date"]),
        -_area_risk(F, bi),
        -_crane_risk(F, bi),
        bi,
    )


def _conflict_closure(prob_info: dict, F: Features, assignments: dict,
                      res: dict, round_idx: int) -> set[int]:
    """Expand official checker violations into a destroy-and-reinsert set."""
    blocks = prob_info["blocks"]
    n = len(blocks)
    violations = res.get("violations") or []
    seeds: set[int] = set()
    times: list[int] = []
    for v in violations:
        for m in _BLOCK_RE.finditer(v):
            bi = int(m.group(1))
            if 0 <= bi < n:
                seeds.add(bi)
        tm = _TIME_RE.search(v)
        if tm:
            times.append(int(tm.group(1)))

    missing = set(range(n)) - set(assignments)
    seeds |= missing
    if not seeds:
        # Last resort: pick the tightest currently assigned blocks.
        seeds = set(sorted(assignments, key=lambda bi: _repair_order_key(prob_info, F, bi))
                    [:max(1, n // 30)])

    cap = min(n, max(12, n // 10) * (round_idx + 1))
    closure: set[int] = set(seeds)

    # Same bay, overlapping or near-future operations are likely part of the
    # replay chain after one failed EXIT leaves a block physically present.
    windows: list[tuple[int, int, int]] = []
    for bi in seeds:
        a = assignments.get(bi)
        if not a:
            continue
        windows.append((
            int(a["bay_id"]),
            int(a["entry_time"]),
            int(a["exit_time"]),
        ))
    if times:
        for bi in seeds:
            a = assignments.get(bi)
            if a:
                for t in times:
                    windows.append((int(a["bay_id"]), t, t + 1))

    margin = 6 + round_idx * 8
    for oj, a in assignments.items():
        if oj in closure:
            continue
        ob = prob_info["blocks"][oj]
        oe = int(a["entry_time"])
        ox = int(a["exit_time"])
        bay = int(a["bay_id"])
        for w_bay, w_entry, w_exit in windows:
            if bay != w_bay:
                continue
            overlaps = oe < w_exit + margin and w_entry - margin < ox
            future_chain = w_entry <= oe <= w_exit + margin
            if overlaps or future_chain:
                closure.add(oj)
                break
        if len(closure) >= cap:
            break

    if len(closure) > cap:
        closure = set(sorted(closure, key=lambda bi: _repair_order_key(prob_info, F, bi))
                      [:cap])
    return closure


def repair_conflict_closure(prob_info: dict, F: Features, bays: list[Bay],
                            assignments: dict,
                            w1: float, w2: float, w3: float,
                            deadline: float,
                            max_rounds: int = 4) -> tuple[dict, dict, dict, int]:
    """Try conflict-closure destroy/reinsert and accept only full-feasible output.

    Returns (assignments, feasibility_result, solution, repaired_count). If no
    full-feasible repair is found, the best effort assignment is returned with
    its official checker result so the caller can escalate to a safe serial
    fallback.
    """
    current = _copy_assignments(assignments)
    res, sol = evaluate_solution(prob_info, current)
    if res["feasible"]:
        return current, res, sol, 0

    bay_weights = _bay_weights(bays)
    repaired_total = 0
    best_effort = current
    best_res = res
    best_sol = sol

    for round_idx in range(max_rounds):
        if time.time() >= deadline:
            break
        closure = _conflict_closure(prob_info, F, current, res, round_idx)
        if not closure:
            break
        kept = {bi: dict(a) for bi, a in current.items() if bi not in closure}
        bay_placed, bay_schedule, bay_loads = _rebuild_bay_state(kept, prob_info, bays)
        failed: set[int] = set()

        for bi in sorted(closure, key=lambda i: _repair_order_key(prob_info, F, i)):
            if time.time() >= deadline:
                failed.add(bi)
                continue
            blk = prob_info["blocks"][bi]
            best = _safe_fallback_place(
                bi, prob_info, F, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, bay_weights,
                deadline=deadline,
                earliest_entry=int(blk["release_time"]),
                pos_cands_cap=96,
            )
            if best is None:
                failed.add(bi)
                continue
            bid, x, y, oi, e, e_t = best
            bay_placed[bid].append(Block(
                block_id=bi, block_data=blk, x=int(x), y=int(y), orient_idx=int(oi),
            ))
            bay_schedule[bid].append((int(e), int(e_t)))
            bay_loads[bid] += blk["workload"]
            kept[bi] = {
                "block_id": bi, "bay_id": bid,
                "x": int(x), "y": int(y), "orient_idx": int(oi),
                "entry_time": int(e), "exit_time": int(e_t),
            }

        current = kept
        res, sol = evaluate_solution(prob_info, current)
        best_effort, best_res, best_sol = current, res, sol
        repaired_total += len(closure) - len(failed)
        _emit("athena.repair.round",
              round=round_idx + 1,
              closure_size=len(closure),
              failed=len(failed),
              feasible=bool(res["feasible"]),
              stage=str(res.get("stage")))
        if res["feasible"]:
            return current, res, sol, repaired_total

    return best_effort, best_res, best_sol, repaired_total


def build_safe_serial_assignments(prob_info: dict, F: Features, bays: list[Bay],
                                  order: list[int] | None = None
                                  ) -> tuple[dict, int]:
    """Build a guaranteed boundary-safe serial fallback when every block fits."""
    blocks = prob_info["blocks"]
    if order is None:
        order = sorted(
            range(len(blocks)),
            key=lambda i: _repair_order_key(prob_info, F, i),
        )
    assignments: dict = {}
    bay_schedule: list[list[tuple[int, int]]] = [[] for _ in bays]
    missing = 0
    for bi in order:
        best = _safe_serial_place_after_all(bi, prob_info, F, bays, bay_schedule)
        if best is None:
            missing += 1
            continue
        bid, x, y, oi, e, e_t = best
        bay_schedule[bid].append((int(e), int(e_t)))
        assignments[bi] = {
            "block_id": bi, "bay_id": bid,
            "x": int(x), "y": int(y), "orient_idx": int(oi),
            "entry_time": int(e), "exit_time": int(e_t),
        }
    return assignments, missing
