"""Standalone placement-rule experiments for submission/solver.py.

This script intentionally does not modify or import through submission/myalgorithm.py.
It imports submission/solver.py directly, builds fixed assignments, and compares
alternative per-bay greedy placement rules against the current solve_bay baseline.

Stage covered initially:
  A/B fixed-assignment materialization:
    - baseline
    - slack_area block order
    - orient_bbox orientation order
    - slack_orient combined

Outputs JSON and CSV summaries under .claude/scratch by default.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION = ROOT / "submission"
if str(SUBMISSION) not in sys.path:
    sys.path.insert(0, str(SUBMISSION))

# Match the current submission defaults that are read at solver import time, while
# still allowing callers to override them from the environment.
os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
os.environ.setdefault("SOLVER_MASK_SEARCH_R", "8")
os.environ.setdefault("SOLVER_MASK", "1")

import solver  # noqa: E402
from utils import Bay, Block, check_entry, check_feasibility  # noqa: E402


@dataclass(frozen=True)
class Variant:
    name: str
    block_order: str = "baseline"
    orient_order: str = "baseline"


VARIANTS = {
    "baseline": Variant("baseline"),
    "slack_area": Variant("slack_area", block_order="slack_area"),
    "orient_bbox": Variant("orient_bbox", orient_order="bbox_aspect"),
    "slack_orient": Variant(
        "slack_orient", block_order="slack_area", orient_order="bbox_aspect"
    ),
}


ASSIGNMENTS: dict[str, Callable[[dict], dict[int, int]]] = {
    "pref": solver.a_pref,
    "balanced": solver.a_balanced_load,
    "capped": solver.a_pref_capped,
}


def natural_problem_key(path: Path) -> tuple[int, str]:
    stem = path.stem
    if stem.startswith("prob_"):
        try:
            return int(stem.split("_", 1)[1]), stem
        except ValueError:
            pass
    return 10**9, stem


def clear_solver_caches() -> None:
    for name in (
        "_POOL",
        "_LOCAL_FP",
        "_LOCAL_BOX",
        "_ORIENT_BBOX",
        "_LOCAL_MASK",
    ):
        obj = getattr(solver, name, None)
        if isinstance(obj, dict):
            obj.clear()


def bbox_size(block_data: dict, orient: int) -> tuple[float, float, float]:
    mnx, mny, mxx, mxy = solver.orient_bbox(block_data, orient)
    w = mxx - mnx
    h = mxy - mny
    return w, h, max(0.0, w * h)


def orient_fits(block_data: dict, bay: dict, orient: int) -> bool:
    mnx, mny, mxx, mxy = solver.orient_bbox(block_data, orient)
    return (
        math.ceil(max(0.0, -mnx)) + mxx <= bay["width"]
        and math.ceil(max(0.0, -mny)) + mxy <= bay["height"]
    )


def best_bbox_area_for_bay(block_data: dict, bay: dict) -> float:
    areas = [
        bbox_size(block_data, o)[2]
        for o in range(len(block_data["shape"]))
        if orient_fits(block_data, bay, o)
    ]
    if not areas:
        areas = [bbox_size(block_data, o)[2] for o in range(len(block_data["shape"]))]
    return min(areas) if areas else 0.0


def block_order(prob: dict, bay_idx: int, ids: Iterable[int], mode: str) -> list[int]:
    blocks = prob["blocks"]
    if mode == "baseline":
        return sorted(ids, key=lambda i: (blocks[i]["due_date"], blocks[i]["processing_time"]))
    if mode == "slack_area":
        bay = prob["bays"][bay_idx]

        def key(i: int) -> tuple[float, float, float, float, float, int]:
            b = blocks[i]
            latest_start = b["due_date"] - b["processing_time"]
            area = best_bbox_area_for_bay(b, bay)
            return (
                latest_start,
                b["due_date"],
                b["release_time"],
                -area,
                -b["workload"],
                i,
            )

        return sorted(ids, key=key)
    raise ValueError(f"unknown block order mode: {mode}")


def orientation_order(block_data: dict, bay: dict, mode: str) -> list[int]:
    n = len(block_data["shape"])
    if mode == "baseline":
        return list(range(n))
    if mode == "bbox_aspect":
        bay_aspect = bay["width"] / max(1e-9, bay["height"])

        def key(o: int) -> tuple[int, float, float, int]:
            w, h, area = bbox_size(block_data, o)
            aspect = w / max(1e-9, h)
            fit_penalty = 0 if orient_fits(block_data, bay, o) else 1
            return (fit_penalty, area, abs(aspect - bay_aspect), o)

        return sorted(range(n), key=key)
    raise ValueError(f"unknown orientation order mode: {mode}")


def find_slot_ordered(
    bay_obj: Bay,
    present_objs: list[Block],
    overlap_objs: list[Block],
    bd: dict,
    bid: int,
    W: int,
    H: int,
    step: int,
    orient_mode: str,
    bay_dict: dict,
):
    ov_boxes = [ob.bounding_rect() for ob in overlap_objs]
    for o in orientation_order(bd, bay_dict, orient_mode):
        mnx, mny, mxx, mxy = solver.orient_bbox(bd, o)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        lbx0, lby0, lbx1, lby1 = solver._local_box(bd, o)
        for y in range(y_start, y_end + 1, step):
            cy0 = lby0 + y
            cy1 = lby1 + y
            row = [b for b in ov_boxes if cy0 < b[3] and b[1] < cy1]
            for x in range(x_start, x_end + 1, step):
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                if any(cx0 < b[2] and b[0] < cx1 for b in row):
                    continue
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                if check_entry(bay_obj, present_objs, cand):
                    continue
                return x, y, o
    return None


def find_slot_mask_ordered(
    bay_obj: Bay,
    present_objs: list[Block],
    ov_boxmasks: list[tuple],
    bd: dict,
    bid: int,
    W: int,
    H: int,
    step: int,
    mask_R: int,
    orient_mode: str,
    bay_dict: dict,
):
    for o in orientation_order(bd, bay_dict, orient_mode):
        mnx, mny, mxx, mxy = solver.orient_bbox(bd, o)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        lbx0, lby0, lbx1, lby1 = solver._local_box(bd, o)
        cmask = solver._local_mask(bd, o, mask_R)
        for y in range(y_start, y_end + 1, step):
            cy0 = lby0 + y
            cy1 = lby1 + y
            cand_ay = cmask.iy0 + y * mask_R
            row = [bm for bm in ov_boxmasks if cy0 < bm[0][3] and bm[0][1] < cy1]
            for x in range(x_start, x_end + 1, step):
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                cand_ax = cmask.ix0 + x * mask_R
                bad = False
                for ob_box, ob_mask, ob_mix0, ob_miy0 in row:
                    if not (cx0 < ob_box[2] and ob_box[0] < cx1):
                        continue
                    if solver.masks_overlap(cmask, cand_ax, cand_ay, ob_mask, ob_mix0, ob_miy0):
                        bad = True
                        break
                if bad:
                    continue
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                if check_entry(bay_obj, present_objs, cand):
                    continue
                return x, y, o
    return None


def solve_bay_variant(
    prob: dict,
    bay_idx: int,
    ids: list[int],
    variant: Variant,
    step: int = 2,
    tcap: int = 200,
    mask: bool | None = None,
    mask_R: int | None = None,
) -> list[dict]:
    if variant.name == "baseline":
        return solver.solve_bay(
            prob,
            bay_idx,
            ids,
            step=step,
            tcap=tcap,
            mask=solver._MASK_SEARCH if mask is None else mask,
            mask_R=solver._MASK_R_SEARCH if mask_R is None else mask_R,
        )

    use_mask = (solver._MASK_SEARCH if mask is None else mask) and solver._HAS_SHAPELY
    mask_R = solver._MASK_R_SEARCH if mask_R is None else mask_R
    bays = prob["bays"]
    blocks = prob["blocks"]
    bay_dict = bays[bay_idx]
    bay_obj = Bay.from_dict(bay_dict, bay_idx)
    W = bay_dict["width"]
    H = bay_dict["height"]
    placed: list[dict] = []
    order = block_order(prob, bay_idx, ids, variant.block_order)

    for i in order:
        bd = blocks[i]
        release = bd["release_time"]
        proc = bd["processing_time"]
        chosen = None
        for t in range(release, release + tcap):
            present = [
                Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                for p in placed
                if p["entry"] <= t < p["exit"]
            ]
            overlap = [
                Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                for p in placed
                if p["entry"] < t + proc and t < p["exit"]
            ]
            slot = find_slot_ordered(
                bay_obj,
                present,
                overlap,
                bd,
                i,
                W,
                H,
                step,
                variant.orient_order,
                bay_dict,
            )
            if slot is None and use_mask:
                ov_boxmasks = [
                    (p["bb"], p["mask"], p["mix0"], p["miy0"])
                    for p in placed
                    if p["entry"] < t + proc and t < p["exit"]
                ]
                slot = find_slot_mask_ordered(
                    bay_obj,
                    present,
                    ov_boxmasks,
                    bd,
                    i,
                    W,
                    H,
                    step,
                    mask_R,
                    variant.orient_order,
                    bay_dict,
                )
            if slot:
                chosen = (t, slot[0], slot[1], slot[2])
                break
        if chosen is None:
            t = max((p["exit"] for p in placed), default=release)
            for tt in range(t, t + 1000):
                present = [
                    Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                    for p in placed
                    if p["entry"] <= tt < p["exit"]
                ]
                overlap = [
                    Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                    for p in placed
                    if p["entry"] < tt + proc and tt < p["exit"]
                ]
                slot = find_slot_ordered(
                    bay_obj,
                    present,
                    overlap,
                    bd,
                    i,
                    W,
                    H,
                    step,
                    variant.orient_order,
                    bay_dict,
                )
                if slot:
                    chosen = (tt, slot[0], slot[1], slot[2])
                    break
            if chosen is None:
                ordered = orientation_order(bd, bay_dict, variant.orient_order)
                o_fit = next((o for o in ordered if orient_fits(bd, bay_dict, o)), ordered[0])
                mnx, mny, _, _ = solver.orient_bbox(bd, o_fit)
                chosen = (
                    t,
                    math.ceil(max(0.0, -mnx)),
                    math.ceil(max(0.0, -mny)),
                    o_fit,
                )

        rec = {
            "id": i,
            "x": chosen[1],
            "y": chosen[2],
            "o": chosen[3],
            "entry": chosen[0],
            "exit": chosen[0] + proc,
        }
        if use_mask:
            blk_o = Block(i, bd, chosen[1], chosen[2], chosen[3])
            rec["bb"] = blk_o.bounding_rect()
            lm = solver._local_mask(bd, chosen[3], mask_R)
            rec["mask"] = lm
            rec["mix0"] = lm.ix0 + chosen[1] * mask_R
            rec["miy0"] = lm.iy0 + chosen[2] * mask_R
        placed.append(rec)
    return placed


def evaluate_assignment(
    prob: dict,
    assign: dict[int, int],
    variant: Variant,
    validate: bool = False,
) -> dict:
    t0 = time.time()
    packed = []
    obj1 = 0.0
    bay_rows = []
    for bay_idx in range(len(prob["bays"])):
        ids = [i for i, a in assign.items() if a == bay_idx]
        if not ids:
            continue
        bt0 = time.time()
        placed = solve_bay_variant(prob, bay_idx, ids, variant)
        tard, exits = solver.extract_tardiness(prob, bay_idx, placed)
        wall = time.time() - bt0
        packed.append((bay_idx, placed, exits))
        obj1 += tard
        bay_rows.append(
            {
                "solve_bay_method": variant.name,
                "bay": bay_idx,
                "n_blocks": len(ids),
                "tardiness": tard,
                "wall_s": wall,
            }
        )
    obj2, obj3 = solver.obj23(prob, assign)
    w = prob["weights"]
    total = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    feasible = None
    violations = None
    if validate:
        sol = solver._solution_from_packed(packed)
        chk = check_feasibility(prob, sol)
        feasible = bool(chk.get("feasible"))
        violations = chk.get("violations", [])[:3]
        total = chk.get("objective", total) if feasible else total
        obj1 = chk.get("obj1", obj1) if feasible else obj1
        obj2 = chk.get("obj2", obj2) if feasible else obj2
        obj3 = chk.get("obj3", obj3) if feasible else obj3
    return {
        "variant": variant.name,
        "objective": total,
        "obj1": obj1,
        "obj2": obj2,
        "obj3": obj3,
        "wall_s": time.time() - t0,
        "feasible": feasible,
        "violations": violations,
        "bays": bay_rows,
    }


def summarize(rows: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], dict[str, dict]] = {}
    for r in rows:
        key = (r["problem"], r["assignment"])
        by_key.setdefault(key, {})[r["variant"]] = r

    out = []
    for (problem, assignment), variants in sorted(by_key.items()):
        base = variants.get("baseline")
        if not base:
            continue
        for name, r in variants.items():
            if name == "baseline":
                continue
            delta_obj = r["objective"] - base["objective"]
            delta_obj_pct = 100.0 * delta_obj / base["objective"] if base["objective"] else 0.0
            delta_z1 = r["obj1"] - base["obj1"]
            out.append(
                {
                    "problem": problem,
                    "assignment": assignment,
                    "variant": name,
                    "base_objective": base["objective"],
                    "variant_objective": r["objective"],
                    "delta_objective": delta_obj,
                    "delta_objective_pct": delta_obj_pct,
                    "base_z1": base["obj1"],
                    "variant_z1": r["obj1"],
                    "delta_z1": delta_z1,
                    "base_wall_s": base["wall_s"],
                    "variant_wall_s": r["wall_s"],
                    "feasible": r["feasible"],
                }
            )
    return out


def aggregate(summary_rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in summary_rows:
        groups.setdefault(r["variant"], []).append(r)
    out = []
    for variant, rs in sorted(groups.items()):
        base_total = sum(r["base_objective"] for r in rs)
        var_total = sum(r["variant_objective"] for r in rs)
        delta = var_total - base_total
        wins = sum(1 for r in rs if r["delta_objective"] < -1e-9)
        losses = sum(1 for r in rs if r["delta_objective"] > 1e-9)
        ties = len(rs) - wins - losses
        worst = max((r["delta_objective_pct"] for r in rs), default=0.0)
        best = min((r["delta_objective_pct"] for r in rs), default=0.0)
        out.append(
            {
                "variant": variant,
                "cases": len(rs),
                "base_total": base_total,
                "variant_total": var_total,
                "delta_total": delta,
                "delta_total_pct": 100.0 * delta / base_total if base_total else 0.0,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "best_case_pct": best,
                "worst_case_pct": worst,
                "base_z1": sum(r["base_z1"] for r in rs),
                "variant_z1": sum(r["variant_z1"] for r in rs),
                "delta_z1": sum(r["delta_z1"] for r in rs),
            }
        )
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--train-dir", default=str(ROOT / "train"))
    p.add_argument("--problems", nargs="*", default=None, help="problem stems, e.g. prob_1")
    p.add_argument(
        "--assignments",
        nargs="*",
        default=["pref", "balanced", "capped"],
        choices=sorted(ASSIGNMENTS),
    )
    p.add_argument(
        "--variants",
        nargs="*",
        default=["baseline", "slack_area", "orient_bbox", "slack_orient"],
        choices=sorted(VARIANTS),
    )
    p.add_argument("--validate", action="store_true")
    p.add_argument("--out-dir", default=str(ROOT / ".claude" / "scratch" / "placement_experiment"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    train_dir = Path(args.train_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    problem_paths = sorted(train_dir.glob("prob_*.json"), key=natural_problem_key)
    if args.problems:
        wanted = {p if p.endswith(".json") else f"{p}.json" for p in args.problems}
        problem_paths = [p for p in problem_paths if p.name in wanted]
    if not problem_paths:
        raise SystemExit("no problem files selected")

    rows = []
    started = time.strftime("%Y%m%d_%H%M%S")
    print(
        f"running placement experiment: problems={len(problem_paths)} "
        f"assignments={args.assignments} variants={args.variants}"
    )
    for path in problem_paths:
        with path.open("r", encoding="utf-8") as f:
            prob = json.load(f)
        problem_name = prob.get("name") or path.stem
        print(f"[{problem_name}]")
        clear_solver_caches()
        assignments = {name: ASSIGNMENTS[name](prob) for name in args.assignments}
        for assignment_name, assign in assignments.items():
            for variant_name in args.variants:
                variant = VARIANTS[variant_name]
                result = evaluate_assignment(prob, assign, variant, validate=args.validate)
                row = {
                    "problem": problem_name,
                    "assignment": assignment_name,
                    "solve_bay_method": variant.name,
                    **{k: v for k, v in result.items() if k != "bays"},
                }
                rows.append(row)
                print(
                    f"  {assignment_name:8s} {variant_name:12s} "
                    f"obj={result['objective']:.0f} z1={result['obj1']:.0f} "
                    f"wall={result['wall_s']:.2f}s"
                )

    summary_rows = summarize(rows)
    aggregate_rows = aggregate(summary_rows)
    payload = {
        "created_at": started,
        "env": {
            "SOLVER_MASK_SEARCH": os.environ.get("SOLVER_MASK_SEARCH"),
            "SOLVER_MASK_SEARCH_R": os.environ.get("SOLVER_MASK_SEARCH_R"),
            "SOLVER_NUMBA": os.environ.get("SOLVER_NUMBA"),
            "has_numba": bool(getattr(solver, "_HAS_NUMBA", False)),
            "has_shapely": bool(getattr(solver, "_HAS_SHAPELY", False)),
        },
        "rows": rows,
        "summary": summary_rows,
        "aggregate": aggregate_rows,
    }
    json_path = out_dir / f"placement_experiment_{started}.json"
    summary_csv = out_dir / f"placement_experiment_summary_{started}.csv"
    aggregate_csv = out_dir / f"placement_experiment_aggregate_{started}.csv"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_csv(summary_csv, summary_rows)
    write_csv(aggregate_csv, aggregate_rows)

    print("\naggregate:")
    for r in aggregate_rows:
        print(
            f"  {r['variant']:12s} delta={r['delta_total_pct']:+.2f}% "
            f"wins/losses/ties={r['wins']}/{r['losses']}/{r['ties']} "
            f"delta_z1={r['delta_z1']:+.0f}"
        )
    print(f"\nwrote {json_path}")
    print(f"wrote {summary_csv}")
    print(f"wrote {aggregate_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
