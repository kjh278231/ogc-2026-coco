#!/usr/bin/env python3
"""
geometry_debug.py -- Decompose check_feasibility stage failures into actionable
per-block / per-layer / per-time-window details.

The official `check_feasibility` only returns the *earliest* failing stage and
a flat list of human-readable violation strings.  This helper:

  - Re-runs check_entry / check_exit / check_collisions directly on the solution,
    capturing structured `EntryObstruction` / `CollisionResult` records.
  - For each violation, prints:
      * Which block (id, bay, position, orient, time window)
      * Which existing block obstructs it
      * Which (k, j) layer pair caused the obstruction (descent-sweep vs final-pos)
      * Overlap area in bay grid units
  - Groups violations by (failing_block, existing_block) so the same conflict
    isn't reported per-layer-pair when one block-pair causes many.

Two input modes
---------------
  --solution PATH        : drill an already-built solution JSON
  --probe-edd            : run baseline_greedy._place_blocks with EDD sort and
                           NO _repair, capturing the raw greedy failure that
                           triggers stage-2 in run_2/run_3 bench_B5 instances.

Output is plain text with markdown-style headers so the geometry-debug skill
can paraphrase / table-format it.
"""
from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys
import time as _time
from collections import defaultdict

# Windows consoles default to cp949 (Korean locale); force stdout/stderr to UTF-8
# so the report's em-dashes, arrows, etc. don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "baseline"))

# Ensure codex_deps shapely/numpy are visible.
CODEX_DEPS = REPO / ".codex_deps"
if CODEX_DEPS.exists():
    sys.path.insert(0, str(CODEX_DEPS))

from utils import (  # noqa: E402
    Bay,
    Block,
    check_collisions,
    check_entry,
    check_exit,
    check_feasibility,
)
import baseline_greedy  # noqa: E402


# ----------------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------------

def load_instance(path: pathlib.Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_solution(path: pathlib.Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_solution_via_edd_no_repair(prob_info: dict, budget_s: float = 30.0) -> dict:
    """Run _place_blocks with EDD sort, no repair.  Captures raw greedy failure."""
    blocks_data = prob_info["blocks"]
    bays_data = prob_info["bays"]
    n_blocks = len(blocks_data)
    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)
    bays = [Bay.from_dict(d, i) for i, d in enumerate(bays_data)]
    perm = sorted(
        range(n_blocks),
        key=lambda i: (blocks_data[i]["due_date"], blocks_data[i]["processing_time"]),
    )
    t_start = _time.time()
    deadline = t_start + max(1.0, budget_s)
    bay_placed = [[] for _ in range(len(bays))]
    bay_schedule = [[] for _ in range(len(bays))]
    bay_loads = [0.0] * len(bays)
    assignments = baseline_greedy._place_blocks(
        perm, blocks_data, bays, bay_placed, bay_schedule, bay_loads,
        w1, w2, w3, forced_ids=set(),
        t_start=t_start, log_interval=0, deadline=deadline,
    )
    return {"operations": baseline_greedy._build_operations(list(assignments.values()))}


# ----------------------------------------------------------------------------
# Assignment reconstruction
# ----------------------------------------------------------------------------

def build_assignment_lookup(solution: dict) -> dict[int, dict]:
    """{block_id -> {bay, x, y, orient, entry, exit}} from solution ops dict."""
    out: dict[int, dict] = {}
    for t_str, ops in solution.get("operations", {}).items():
        try:
            t = int(t_str)
        except (TypeError, ValueError):
            continue
        for op in ops:
            bid = op.get("block_id")
            if bid is None:
                continue
            d = out.setdefault(bid, {})
            if op.get("type") == "ENTRY":
                d["entry"] = t
                d["bay"] = op.get("bay_id")
                d["x"] = op.get("x", 0)
                d["y"] = op.get("y", 0)
                d["orient"] = op.get("orient_idx", 0)
            elif op.get("type") == "EXIT":
                d["exit"] = t
    return out


def _block_obj(prob_info: dict, bid: int, d: dict) -> Block:
    blk = Block(
        block_id=bid,
        block_data=prob_info["blocks"][bid],
        x=int(d["x"]), y=int(d["y"]),
        orient_idx=int(d["orient"]),
    )
    blk.entry_time = d.get("entry")
    blk.exit_time = d.get("exit")
    return blk


# ----------------------------------------------------------------------------
# Stage drill-down
# ----------------------------------------------------------------------------

def print_header(prob_info: dict, solution: dict, feasres: dict) -> None:
    n_blocks = len(prob_info["blocks"])
    n_bays = len(prob_info["bays"])
    n_ops_entry = sum(1 for ops in solution.get("operations", {}).values()
                      for op in ops if op.get("type") == "ENTRY")
    n_ops_exit = sum(1 for ops in solution.get("operations", {}).values()
                     for op in ops if op.get("type") == "EXIT")
    print(f"# Geometry Debug — instance={prob_info.get('name', '?')}")
    print(f"- bays={n_bays}, blocks={n_blocks}, ENTRY ops={n_ops_entry}, EXIT ops={n_ops_exit}")
    print(f"- check_feasibility -> feasible={feasres['feasible']}, stage={feasres['stage']}, "
          f"#violations={len(feasres.get('violations') or [])}")


def report_stage1(feasres: dict) -> None:
    print("\n## Stage 1 — Assignment validity")
    vs = feasres.get("violations") or []
    if not vs:
        print("_(no stage-1 violations reported)_")
        return
    for v in vs:
        print(f"- {v}")


def report_stage2(prob_info: dict, solution: dict, limit: int = 25) -> None:
    print("\n## Stage 2 — Crane entry feasibility")
    bays = [Bay.from_dict(d, i) for i, d in enumerate(prob_info["bays"])]
    lookup = build_assignment_lookup(solution)
    fail_blocks = []
    for bid, d in sorted(lookup.items()):
        if "entry" not in d or d.get("bay") is None:
            continue
        bay_idx = d["bay"]
        if bay_idx >= len(bays):
            continue
        entry_t = d["entry"]
        present = []
        for ob, od in lookup.items():
            if ob == bid: continue
            if od.get("bay") != bay_idx: continue
            if "entry" not in od or "exit" not in od: continue
            if od["entry"] <= entry_t < od["exit"]:
                present.append(_block_obj(prob_info, ob, od))
        new_blk = _block_obj(prob_info, bid, d)
        try:
            obs = check_entry(bays[bay_idx], present, new_blk, fast=False)
        except Exception as e:
            print(f"- block {bid}: check_entry raised {type(e).__name__}: {e}")
            continue
        if obs:
            fail_blocks.append((bid, d, obs, new_blk))

    if not fail_blocks:
        print("_(no stage-2 violations)_")
        return
    print(f"_Total blocks with stage-2 violations: {len(fail_blocks)} (showing up to {limit})_\n")
    for bid, d, obs, new_blk in fail_blocks[:limit]:
        print(f"### Block {bid} ENTRY @ t={d['entry']} bay={d['bay']} "
              f"pos=({d['x']},{d['y']}) orient={d['orient']}")
        grouped: dict[object, list] = defaultdict(list)
        for o in obs:
            key = "BOUNDARY" if o.existing_block is new_blk else o.existing_block.block_id
            grouped[key].append(o)
        for key, recs in grouped.items():
            if key == "BOUNDARY":
                area = getattr(recs[0].intersection, "area", float("nan"))
                print(f"  - BOUNDARY: footprint outside bay (outside_area={area:.2f})")
                continue
            ks = sorted({r.new_layer for r in recs})
            js = sorted({r.exist_layer for r in recs})
            sweep_count = sum(1 for r in recs if r.exist_layer > r.new_layer)
            final_count = sum(1 for r in recs if r.exist_layer == r.new_layer)
            max_area = max((getattr(r.intersection, "area", 0.0) for r in recs), default=0.0)
            print(f"  - existing block {key}: layers k(new)={ks} j(exist)={js} "
                  f"[final={final_count}, descent-sweep={sweep_count}] max_overlap={max_area:.2f}")


def report_stage3(prob_info: dict, solution: dict, limit: int = 25) -> None:
    print("\n## Stage 3 — Crane exit feasibility")
    bays = [Bay.from_dict(d, i) for i, d in enumerate(prob_info["bays"])]
    lookup = build_assignment_lookup(solution)
    fail_blocks = []
    for bid, d in sorted(lookup.items()):
        if "exit" not in d or d.get("bay") is None:
            continue
        bay_idx = d["bay"]
        if bay_idx >= len(bays):
            continue
        exit_t = d["exit"]
        present = [_block_obj(prob_info, bid, d)]  # target itself
        for ob, od in lookup.items():
            if ob == bid: continue
            if od.get("bay") != bay_idx: continue
            if "entry" not in od or "exit" not in od: continue
            if od["entry"] < exit_t < od["exit"]:
                present.append(_block_obj(prob_info, ob, od))
        target = present[0]
        try:
            obs = check_exit(bays[bay_idx], present, target, fast=False)
        except Exception as e:
            print(f"- block {bid}: check_exit raised {type(e).__name__}: {e}")
            continue
        if obs:
            fail_blocks.append((bid, d, obs, target))
    if not fail_blocks:
        print("_(no stage-3 violations)_")
        return
    print(f"_Total blocks with stage-3 violations: {len(fail_blocks)} (showing up to {limit})_\n")
    for bid, d, obs, target in fail_blocks[:limit]:
        print(f"### Block {bid} EXIT @ t={d['exit']} bay={d['bay']}")
        grouped: dict[object, list] = defaultdict(list)
        for o in obs:
            key = "BOUNDARY" if o.existing_block is target else o.existing_block.block_id
            grouped[key].append(o)
        for key, recs in grouped.items():
            if key == "BOUNDARY":
                print("  - BOUNDARY: target footprint outside bay")
                continue
            ks = sorted({r.new_layer for r in recs})
            js = sorted({r.exist_layer for r in recs})
            sweep_count = sum(1 for r in recs if r.exist_layer > r.new_layer)
            final_count = sum(1 for r in recs if r.exist_layer == r.new_layer)
            print(f"  - existing block {key}: target_k={ks} existing_j={js} "
                  f"[final={final_count}, ascent-sweep={sweep_count}]")


def report_stage4(prob_info: dict, solution: dict, limit: int = 25) -> None:
    print("\n## Stage 4 — Spatial collision / boundary")
    bays = [Bay.from_dict(d, i) for i, d in enumerate(prob_info["bays"])]
    lookup = build_assignment_lookup(solution)
    by_bay: dict[int, list[Block]] = defaultdict(list)
    for bid, d in lookup.items():
        if d.get("bay") is None or "entry" not in d or "exit" not in d:
            continue
        by_bay[d["bay"]].append(_block_obj(prob_info, bid, d))
    fail_rows = []
    for bay_idx, blks in by_bay.items():
        if bay_idx >= len(bays):
            continue
        # Walk pairs with time overlap; reuse check_collisions per pair-set.
        # We just call check_collisions for the full bay set; it returns per-pair records.
        try:
            results = check_collisions(bays[bay_idx], blks, layer_indices=None)
        except Exception as e:
            print(f"- bay {bay_idx}: check_collisions raised {type(e).__name__}: {e}")
            continue
        for r in results:
            fail_rows.append((bay_idx, r))
        # Boundary check
        for b in blks:
            if not bays[bay_idx].contains_block(b):
                print(f"- bay {bay_idx} BOUNDARY violation: block {b.block_id} "
                      f"@ ({b.x},{b.y}) orient={b.orient_idx}")
    if not fail_rows:
        print("_(no stage-4 pairwise overlaps reported by check_collisions)_")
        return
    print(f"_Total pairwise overlap records: {len(fail_rows)} (showing up to {limit})_\n")
    for bay_idx, r in fail_rows[:limit]:
        a_id = getattr(r.block_a, "block_id", "?")
        b_id = getattr(r.block_b, "block_id", "?")
        layer = getattr(r, "layer", "?")
        area = getattr(r.intersection, "area", float("nan")) if hasattr(r, "intersection") else float("nan")
        print(f"  - bay {bay_idx}: blocks {a_id}↔{b_id} at layer={layer}, overlap_area={area:.2f}")


def report_stage5(feasres: dict) -> None:
    print("\n## Stage 5 — Sequential operation replay")
    vs = feasres.get("violations") or []
    if not vs:
        print("_(stage 5 not reached or no replay violations)_")
        return
    for v in vs:
        print(f"- {v}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", required=True, type=pathlib.Path,
                    help="Instance JSON path (alg_tester/example/benchmark/*.json)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--solution", type=pathlib.Path,
                     help="Solution JSON path (e.g. dumped from a failed run)")
    src.add_argument("--probe-edd", action="store_true",
                     help="Build a solution via _place_blocks with EDD sort and NO repair")
    ap.add_argument("--probe-budget", type=float, default=30.0,
                    help="Wall-clock budget (s) for --probe-edd (default 30)")
    ap.add_argument("--limit", type=int, default=25,
                    help="Max violation rows to print per stage (default 25)")
    ap.add_argument("--dump-solution", type=pathlib.Path, default=None,
                    help="If set with --probe-edd, write the captured solution JSON here")
    args = ap.parse_args()

    prob_info = load_instance(args.instance)
    if args.probe_edd:
        print(f"[probe-edd] running _place_blocks (EDD sort, no repair, budget={args.probe_budget}s)...",
              file=sys.stderr)
        solution = build_solution_via_edd_no_repair(prob_info, args.probe_budget)
        if args.dump_solution is not None:
            args.dump_solution.parent.mkdir(parents=True, exist_ok=True)
            with open(args.dump_solution, "w", encoding="utf-8") as f:
                json.dump(solution, f, indent=2, default=str)
            print(f"[probe-edd] solution dumped to {args.dump_solution}", file=sys.stderr)
    else:
        solution = load_solution(args.solution)

    feasres = check_feasibility(prob_info, solution)
    print_header(prob_info, solution, feasres)

    stage = feasres.get("stage")
    # Always print the failing stage's drill-down; also print preceding stage hooks
    # for context when stage>1 (assignment validity is universal).
    if stage in (1, 0):
        report_stage1(feasres)
    elif stage == 2:
        report_stage1(feasres)  # quick check for assignment health
        report_stage2(prob_info, solution, limit=args.limit)
    elif stage == 3:
        report_stage3(prob_info, solution, limit=args.limit)
    elif stage == 4:
        report_stage4(prob_info, solution, limit=args.limit)
    elif stage == 5:
        report_stage5(feasres)
    else:
        # Feasible — still show stage 2/3/4 drills as a no-op check (useful for sanity)
        print("\n_(solution is feasible; running drill-downs as sanity check)_")
        report_stage2(prob_info, solution, limit=args.limit)
        report_stage3(prob_info, solution, limit=args.limit)
        report_stage4(prob_info, solution, limit=args.limit)

    if feasres.get("feasible"):
        obj = feasres.get("objective")
        print(f"\n_Solution feasible. objective={obj}_")
    else:
        print(f"\n_Solution infeasible at stage {stage}._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
