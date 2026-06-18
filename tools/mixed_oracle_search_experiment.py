"""Sequential mixed-oracle search experiment.

This is a standalone harness. It does not modify submission/solver.py or existing
run scripts. It tests the idea of spending a fixed objective-evaluation budget
across different solve_bay placement oracles, e.g.:

  baseline 100 evals -> slack_area 100 evals -> slack_orient 100 evals

Important: oracle scores from different placement rules are not assumed to be
commensurate. The search uses the active oracle only for local acceptance, then
every schedule's final assignment is rescored with the same current final
materializer (solver._score_and_pack).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
SUBMISSION = ROOT / "submission"
for p in (str(TOOLS), str(SUBMISSION)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Must be set before placement_experiment imports solver.
os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
os.environ.setdefault("SOLVER_MASK_SEARCH_R", "8")
os.environ.setdefault("SOLVER_MASK", "1")
os.environ.setdefault("SOLVER_NUMBA", "1")

import placement_experiment as pe  # noqa: E402

solver = pe.solver


SCHEDULES: dict[str, list[tuple[str, int]]] = {
    "base300": [("baseline", 300)],
    "slack_area300": [("slack_area", 300)],
    "slack_orient300": [("slack_orient", 300)],
    "mix_b_s_so": [("baseline", 100), ("slack_area", 100), ("slack_orient", 100)],
    "mix_s_so_b": [("slack_area", 100), ("slack_orient", 100), ("baseline", 100)],
}


def natural_problem_key(path: Path) -> tuple[int, str]:
    stem = path.stem
    if stem.startswith("prob_"):
        try:
            return int(stem.split("_", 1)[1]), stem
        except ValueError:
            pass
    return 10**9, stem


def assignment_signature(assign: dict[int, int]) -> tuple[tuple[int, int], ...]:
    return tuple(sorted(assign.items()))


class OracleEvaluator:
    def __init__(self, prob: dict, variant_name: str):
        self.prob = prob
        self.variant_name = variant_name
        self.variant = pe.VARIANTS[variant_name]
        self.cache: dict[tuple[str, int, tuple[int, ...]], float] = {}
        self.evals = 0
        self.wall_s = 0.0

    def score(self, assign: dict[int, int]) -> tuple[float, dict[int, float]]:
        t0 = time.time()
        self.evals += 1
        m = len(self.prob["bays"])
        obj1 = 0.0
        perbay: dict[int, float] = {}
        for j in range(m):
            ids = tuple(sorted(i for i, a in assign.items() if a == j))
            if not ids:
                perbay[j] = 0.0
                continue
            key = (self.variant_name, j, ids)
            T = self.cache.get(key)
            if T is None:
                placed = pe.solve_bay_variant(self.prob, j, list(ids), self.variant)
                T, _ = solver.extract_tardiness(self.prob, j, placed)
                self.cache[key] = T
            perbay[j] = T
            obj1 += T
        obj2, obj3 = solver.obj23(self.prob, assign)
        w = self.prob["weights"]
        self.wall_s += time.time() - t0
        return w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3, perbay


def best_seed(prob: dict) -> tuple[str, dict[int, int], float]:
    evaluator = OracleEvaluator(prob, "baseline")
    candidates = {
        "pref": solver.a_pref(prob),
        "balanced": solver.a_balanced_load(prob),
        "capped": solver.a_pref_capped(prob),
    }
    best_name = ""
    best_assign = None
    best_score = float("inf")
    for name, assign in candidates.items():
        score, _ = evaluator.score(assign)
        if score < best_score:
            best_name = name
            best_assign = dict(assign)
            best_score = score
    assert best_assign is not None
    return best_name, best_assign, best_score


def move_targets(prob: dict, assign: dict[int, int], perbay: dict[int, float]) -> list[tuple[int, list[int]]]:
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    loads = [0.0] * m
    for i, j in assign.items():
        loads[j] += blocks[i]["workload"]
    maxload = max(range(m), key=lambda j: u[j] * loads[j])
    pref_bay = {
        i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
        for i in range(len(blocks))
    }
    tardy = {j for j in range(m) if perbay.get(j, 0.0) > 0}
    movers = [
        i
        for i in assign
        if assign[i] in tardy or assign[i] == maxload or assign[i] != pref_bay[i]
    ]
    if not movers:
        movers = list(assign)
    out = []
    for i in movers:
        targets = sorted(
            (j for j in range(m) if j != assign[i] and solver.fits(blocks[i], bays[j])),
            key=lambda j: -blocks[i]["bay_preferences"][j],
        )
        if targets:
            out.append((i, targets))
    return out


def perturb_assignment(prob: dict, assign: dict[int, int], rng: random.Random) -> dict[int, int]:
    blocks = prob["blocks"]
    bays = prob["bays"]
    cand = dict(assign)
    ids = list(cand)
    if not ids:
        return cand
    k = min(len(ids), rng.randint(2, 5))
    for i in rng.sample(ids, k):
        opts = [j for j in range(len(bays)) if j != cand[i] and solver.fits(blocks[i], bays[j])]
        if opts:
            cand[i] = rng.choice(opts)
    return cand


def run_phase(
    prob: dict,
    assign: dict[int, int],
    variant_name: str,
    eval_budget: int,
) -> tuple[dict[int, int], dict]:
    evaluator = OracleEvaluator(prob, variant_name)
    rng = random.Random(0)
    best = dict(assign)
    best_score, best_perbay = evaluator.score(best)
    cur = dict(best)
    cur_score, perbay = best_score, best_perbay
    improvements = 0
    kicks = 0
    passes = 0
    exhausted = False

    while evaluator.evals < eval_budget:
        passes += 1
        improved_this_pass = False
        moves = move_targets(prob, cur, perbay)
        if not moves:
            break
        for i, targets in moves:
            for j in targets:
                if evaluator.evals >= eval_budget:
                    exhausted = True
                    break
                trial = dict(cur)
                trial[i] = j
                score, trial_perbay = evaluator.score(trial)
                if score < cur_score - 1e-9:
                    cur = trial
                    cur_score = score
                    perbay = trial_perbay
                    if score < best_score - 1e-9:
                        best = dict(trial)
                        best_score = score
                        best_perbay = trial_perbay
                        improvements += 1
                    improved_this_pass = True
                    break
            if exhausted or improved_this_pass:
                break
        if exhausted:
            break
        if not improved_this_pass:
            if evaluator.evals >= eval_budget:
                break
            kicked = perturb_assignment(prob, best, rng)
            kick_score, kick_perbay = evaluator.score(kicked)
            kicks += 1
            if kick_score < best_score - 1e-9:
                best = dict(kicked)
                best_score = kick_score
                best_perbay = kick_perbay
                improvements += 1
                cur = dict(best)
                cur_score = best_score
                perbay = best_perbay
            else:
                cur = kicked
                cur_score = kick_score
                perbay = kick_perbay

    return best, {
        "variant": variant_name,
        "budget": eval_budget,
        "evals": evaluator.evals,
        "oracle_score": best_score,
        "improvements": improvements,
        "kicks": kicks,
        "passes": passes,
        "wall_s": evaluator.wall_s,
        "cache_size": len(evaluator.cache),
        "cache_key": "solve_bay_method,bay,block_set",
    }


def final_score(prob: dict, assign: dict[int, int]) -> dict:
    t0 = time.time()
    score, packed = solver._score_and_pack(prob, assign)
    sol = solver._solution_from_packed(packed)
    chk = pe.check_feasibility(prob, sol)
    return {
        "objective": chk.get("objective", score) if chk.get("feasible") else score,
        "obj1": chk.get("obj1"),
        "obj2": chk.get("obj2"),
        "obj3": chk.get("obj3"),
        "feasible": bool(chk.get("feasible")),
        "stage": chk.get("stage"),
        "violations": chk.get("violations", [])[:3],
        "wall_s": time.time() - t0,
    }


def run_schedule(prob: dict, schedule_name: str, schedule: list[tuple[str, int]]) -> dict:
    seed_name, seed_assign, seed_oracle_score = best_seed(prob)
    assign = dict(seed_assign)
    phase_rows = []
    t0 = time.time()
    for variant_name, budget in schedule:
        assign, phase = run_phase(prob, assign, variant_name, budget)
        phase_rows.append(phase)
    f = final_score(prob, assign)
    return {
        "schedule": schedule_name,
        "seed": seed_name,
        "seed_oracle_score": seed_oracle_score,
        "final": f,
        "phase_rows": phase_rows,
        "total_phase_evals": sum(p["evals"] for p in phase_rows),
        "total_phase_wall_s": sum(p["wall_s"] for p in phase_rows),
        "wall_s": time.time() - t0,
        "assignment_signature": assignment_signature(assign),
    }


def flatten_rows(problem: str, result: dict) -> dict:
    f = result["final"]
    return {
        "problem": problem,
        "schedule": result["schedule"],
        "seed": result["seed"],
        "objective": f["objective"],
        "obj1": f["obj1"],
        "obj2": f["obj2"],
        "obj3": f["obj3"],
        "feasible": f["feasible"],
        "total_phase_evals": result["total_phase_evals"],
        "total_phase_wall_s": result["total_phase_wall_s"],
        "final_wall_s": f["wall_s"],
        "wall_s": result["wall_s"],
        "phases": " -> ".join(f"{p['variant']}:{p['evals']}/{p['budget']}" for p in result["phase_rows"]),
        "improvements": sum(p["improvements"] for p in result["phase_rows"]),
    }


def summarize(rows: list[dict], baseline_schedule: str = "base300") -> list[dict]:
    by_problem: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_problem.setdefault(r["problem"], {})[r["schedule"]] = r
    out = []
    for problem, scheds in sorted(by_problem.items(), key=lambda kv: natural_problem_key(Path(kv[0] + ".json"))):
        base = scheds.get(baseline_schedule)
        if not base:
            continue
        for name, r in scheds.items():
            if name == baseline_schedule:
                continue
            delta = r["objective"] - base["objective"]
            out.append(
                {
                    "problem": problem,
                    "schedule": name,
                    "base_objective": base["objective"],
                    "objective": r["objective"],
                    "delta_objective": delta,
                    "delta_objective_pct": 100.0 * delta / base["objective"] if base["objective"] else 0.0,
                    "base_obj1": base["obj1"],
                    "obj1": r["obj1"],
                    "delta_obj1": (r["obj1"] or 0) - (base["obj1"] or 0),
                    "base_wall_s": base["wall_s"],
                    "wall_s": r["wall_s"],
                    "base_evals": base["total_phase_evals"],
                    "evals": r["total_phase_evals"],
                    "feasible": r["feasible"],
                }
            )
    return out


def aggregate(summary_rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in summary_rows:
        groups.setdefault(r["schedule"], []).append(r)
    out = []
    for name, rs in sorted(groups.items()):
        base_total = sum(float(r["base_objective"]) for r in rs)
        total = sum(float(r["objective"]) for r in rs)
        delta = total - base_total
        wins = sum(1 for r in rs if float(r["delta_objective"]) < -1e-9)
        losses = sum(1 for r in rs if float(r["delta_objective"]) > 1e-9)
        ties = len(rs) - wins - losses
        out.append(
            {
                "schedule": name,
                "cases": len(rs),
                "base_total": base_total,
                "total": total,
                "delta_total": delta,
                "delta_total_pct": 100.0 * delta / base_total if base_total else 0.0,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "delta_obj1": sum(float(r["delta_obj1"]) for r in rs),
                "avg_wall_s": sum(float(r["wall_s"]) for r in rs) / len(rs),
                "avg_base_wall_s": sum(float(r["base_wall_s"]) for r in rs) / len(rs),
                "avg_evals": sum(float(r["evals"]) for r in rs) / len(rs),
                "avg_base_evals": sum(float(r["base_evals"]) for r in rs) / len(rs),
                "worst_case_pct": max(float(r["delta_objective_pct"]) for r in rs),
                "best_case_pct": min(float(r["delta_objective_pct"]) for r in rs),
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
    p.add_argument("--problems", nargs="*", default=None)
    p.add_argument("--schedules", nargs="*", default=list(SCHEDULES), choices=sorted(SCHEDULES))
    p.add_argument("--out-dir", default=str(ROOT / ".claude" / "scratch" / "mixed_oracle_search"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    train_dir = Path(args.train_dir)
    paths = sorted(train_dir.glob("prob_*.json"), key=natural_problem_key)
    if args.problems:
        wanted = {p if p.endswith(".json") else f"{p}.json" for p in args.problems}
        paths = [p for p in paths if p.name in wanted]
    if not paths:
        raise SystemExit("no problem files selected")

    started = time.strftime("%Y%m%d_%H%M%S")
    rows = []
    raw_results = []
    print(
        f"mixed oracle search: problems={len(paths)} schedules={args.schedules} "
        f"runner={sys.executable} numba={solver._HAS_NUMBA and bool(os.environ.get('SOLVER_NUMBA'))}"
    )
    for path in paths:
        with path.open("r", encoding="utf-8") as f:
            prob = json.load(f)
        problem = prob.get("name") or path.stem
        print(f"[{problem}]")
        pe.clear_solver_caches()
        for sched_name in args.schedules:
            result = run_schedule(prob, sched_name, SCHEDULES[sched_name])
            flat = flatten_rows(problem, result)
            rows.append(flat)
            raw_results.append({"problem": problem, **result})
            print(
                f"  {sched_name:15s} obj={flat['objective']:.0f} z1={flat['obj1']:.0f} "
                f"evals={flat['total_phase_evals']} wall={flat['wall_s']:.2f}s "
                f"phases={flat['phases']}"
            )
    summary_rows = summarize(rows)
    aggregate_rows = aggregate(summary_rows)
    payload = {
        "created_at": started,
        "env": {
            "python": sys.executable,
            "SOLVER_NUMBA": os.environ.get("SOLVER_NUMBA"),
            "SOLVER_MASK_SEARCH": os.environ.get("SOLVER_MASK_SEARCH"),
            "SOLVER_MASK_SEARCH_R": os.environ.get("SOLVER_MASK_SEARCH_R"),
            "has_numba": bool(getattr(solver, "_HAS_NUMBA", False)),
            "numba_on": bool(getattr(solver, "_NUMBA_ON", False)),
            "has_shapely": bool(getattr(solver, "_HAS_SHAPELY", False)),
        },
        "rows": rows,
        "summary": summary_rows,
        "aggregate": aggregate_rows,
        "raw_results": raw_results,
    }
    json_path = out_dir / f"mixed_oracle_search_{started}.json"
    rows_csv = out_dir / f"mixed_oracle_search_rows_{started}.csv"
    summary_csv = out_dir / f"mixed_oracle_search_summary_{started}.csv"
    aggregate_csv = out_dir / f"mixed_oracle_search_aggregate_{started}.csv"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_csv(rows_csv, rows)
    write_csv(summary_csv, summary_rows)
    write_csv(aggregate_csv, aggregate_rows)

    print("\naggregate vs base300:")
    for r in aggregate_rows:
        print(
            f"  {r['schedule']:15s} delta={r['delta_total_pct']:+.2f}% "
            f"wins/losses/ties={r['wins']}/{r['losses']}/{r['ties']} "
            f"avg_wall={r['avg_wall_s']:.2f}s base_avg_wall={r['avg_base_wall_s']:.2f}s "
            f"avg_evals={r['avg_evals']:.1f}"
        )
    print(f"\nwrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
