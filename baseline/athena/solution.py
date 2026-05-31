"""Canonical solution conversion and official feasibility evaluation."""
from __future__ import annotations

from utils import check_feasibility

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
