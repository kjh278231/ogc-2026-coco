"""
Generate high-complexity benchmark instances for OGC 2026 COCO.

This generator is intended for algorithm stress testing, not for reproducing the
hidden instances exactly. It uses the real geometry in example_B2_b10.json as
seed shapes, then creates larger and more diverse problems by:

- increasing the number of bays,
- increasing the number of blocks,
- scaling existing polygonal layer shapes,
- creating tighter release/due schedules,
- increasing temporal overlap to stress spatial and crane constraints,
- varying preference/workload trade-offs.

Generated files are written under:
    alg_tester/example/benchmark/

Usage:
    python alg_tester/example/generate_benchmark_suite.py

Generate one custom case:
    python alg_tester/example/generate_benchmark_suite.py \
        --single --name my_dense --bays 5 --blocks 120 --profile dense_geometry

Recommended first run for local testing:
    python alg_tester/example/generate_benchmark_suite.py --suite smoke

Profiles:
    balanced          General-purpose mixed benchmark.
    tight_due          Tardiness-heavy schedule pressure.
    dense_geometry     Many simultaneous blocks; geometry/placement pressure.
    crane_trap         Long overlap and multi-layer pressure for crane checks.
    preference_skew    Bay preference conflicts with workload/spatial balance.
    workload_balance   Objective trade-off around weighted workload balance.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


BASE_FILE = Path(__file__).with_name("example_B2_b10.json")
OUTPUT_DIR = Path(__file__).with_name("benchmark")


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    bays: int
    blocks: int
    profile: str
    seed: int


DEFAULT_SUITE: list[BenchmarkCase] = [
    BenchmarkCase("bench_B3_b30_balanced", 3, 30, "balanced", 202601),
    BenchmarkCase("bench_B3_b50_tight_due", 3, 50, "tight_due", 202602),
    BenchmarkCase("bench_B4_b70_dense_geometry", 4, 70, "dense_geometry", 202603),
    BenchmarkCase("bench_B4_b90_crane_trap", 4, 90, "crane_trap", 202604),
    BenchmarkCase("bench_B5_b120_preference_skew", 5, 120, "preference_skew", 202605),
    BenchmarkCase("bench_B5_b150_mixed_hard", 5, 150, "balanced", 202606),
]

SMOKE_SUITE: list[BenchmarkCase] = [
    BenchmarkCase("smoke_B3_b20_balanced", 3, 20, "balanced", 202611),
    BenchmarkCase("smoke_B3_b30_dense_geometry", 3, 30, "dense_geometry", 202612),
]


BAY_TEMPLATES = [
    {"width": 85, "height": 20},
    {"width": 71, "height": 22},
    {"width": 92, "height": 18},
    {"width": 64, "height": 24},
    {"width": 108, "height": 17},
    {"width": 78, "height": 26},
]


PROFILE_CONFIG = {
    "balanced": {
        "scale_range": (0.85, 1.18),
        "release_window_factor": 0.45,
        "release_batch": 4,
        "proc_delta": (-1, 2),
        "slack_range": (3, 8),
        "weights": {"w1": 2667, "w2": 7, "w3": 20},
    },
    "tight_due": {
        "scale_range": (0.82, 1.08),
        "release_window_factor": 0.36,
        "release_batch": 3,
        "proc_delta": (0, 3),
        "slack_range": (0, 3),
        "weights": {"w1": 6000, "w2": 5, "w3": 15},
    },
    "dense_geometry": {
        "scale_range": (1.00, 1.28),
        "release_window_factor": 0.24,
        "release_batch": 5,
        "proc_delta": (1, 4),
        "slack_range": (2, 6),
        "weights": {"w1": 3000, "w2": 8, "w3": 15},
    },
    "crane_trap": {
        "scale_range": (0.95, 1.22),
        "release_window_factor": 0.20,
        "release_batch": 6,
        "proc_delta": (2, 5),
        "slack_range": (1, 5),
        "weights": {"w1": 3500, "w2": 6, "w3": 15},
    },
    "preference_skew": {
        "scale_range": (0.82, 1.12),
        "release_window_factor": 0.40,
        "release_batch": 4,
        "proc_delta": (-1, 2),
        "slack_range": (3, 9),
        "weights": {"w1": 1800, "w2": 9, "w3": 80},
    },
    "workload_balance": {
        "scale_range": (0.82, 1.15),
        "release_window_factor": 0.42,
        "release_batch": 4,
        "proc_delta": (-1, 2),
        "slack_range": (3, 8),
        "weights": {"w1": 1800, "w2": 40, "w3": 20},
    },
}


def round4(x: float) -> float:
    return round(float(x), 4)


def iter_points(shape: list[dict]) -> Iterable[tuple[float, float]]:
    for orient in shape:
        for layer in orient["layers"]:
            for x, y in layer:
                yield float(x), float(y)


def bbox_of_orientation(orientation: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for layer in orientation["layers"]:
        for x, y in layer:
            xs.append(float(x))
            ys.append(float(y))
    return min(xs), min(ys), max(xs), max(ys)


def orientation_fits_integer_reference(orientation: dict, bay: dict) -> bool:
    """Check whether an orientation can fit in a bay with integer x/y reference."""
    min_x, min_y, max_x, max_y = bbox_of_orientation(orientation)
    required_w = math.ceil(max(0.0, -min_x)) + max_x
    required_h = math.ceil(max(0.0, -min_y)) + max_y
    return required_w <= bay["width"] and required_h <= bay["height"]


def shape_fits_any_bay(shape: list[dict], bays: list[dict]) -> bool:
    return any(
        orientation_fits_integer_reference(orient, bay)
        for orient in shape
        for bay in bays
    )


def scale_shape(shape: list[dict], scale: float) -> list[dict]:
    scaled = copy.deepcopy(shape)
    for orient in scaled:
        for layer in orient["layers"]:
            for p in layer:
                p[0] = round4(float(p[0]) * scale)
                p[1] = round4(float(p[1]) * scale)
    return scaled


def safe_scaled_shape(shape: list[dict], scale: float, bays: list[dict]) -> list[dict]:
    """Scale shape, reducing scale if needed so at least one orientation fits."""
    for factor in [1.0, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65]:
        candidate = scale_shape(shape, scale * factor)
        if shape_fits_any_bay(candidate, bays):
            return candidate
    # Last resort: keep original geometry, which is known to fit the original example.
    return copy.deepcopy(shape)


def normalize_to_100(raw_scores: list[float]) -> list[int]:
    total = sum(max(0.0001, s) for s in raw_scores)
    floats = [100.0 * max(0.0001, s) / total for s in raw_scores]
    ints = [int(math.floor(v)) for v in floats]
    remain = 100 - sum(ints)
    order = sorted(range(len(raw_scores)), key=lambda i: floats[i] - ints[i], reverse=True)
    for i in order[:remain]:
        ints[i] += 1
    return ints


def make_bays(m: int, profile: str) -> list[dict]:
    if m < 2:
        raise ValueError("At least 2 bays are required.")
    if m > len(BAY_TEMPLATES):
        raise ValueError(f"At most {len(BAY_TEMPLATES)} bays are supported by this generator.")

    bays = copy.deepcopy(BAY_TEMPLATES[:m])

    if profile == "dense_geometry":
        # Slightly compact bays to make packing harder while keeping feasibility realistic.
        for bay in bays:
            bay["width"] = max(55, int(round(bay["width"] * 0.92)))
            bay["height"] = max(16, int(round(bay["height"] * 0.94)))
    elif profile == "crane_trap":
        for bay in bays:
            bay["width"] = max(55, int(round(bay["width"] * 0.96)))
    elif profile == "workload_balance":
        # Mixed bay areas make normalized workload balancing more meaningful.
        for j, bay in enumerate(bays):
            if j % 2 == 0:
                bay["width"] = int(round(bay["width"] * 1.08))
            else:
                bay["height"] = int(round(bay["height"] * 0.92))
    return bays


def make_preferences(m: int, idx: int, rng: random.Random, profile: str) -> list[int]:
    if profile == "preference_skew":
        preferred = idx % m
        raw = [8.0 + rng.random() * 8.0 for _ in range(m)]
        raw[preferred] = 80.0 + rng.random() * 20.0
        return normalize_to_100(raw)

    if profile == "workload_balance":
        # Preferences are deliberately weak so workload balance can dominate.
        raw = [35.0 + rng.random() * 15.0 for _ in range(m)]
        return normalize_to_100(raw)

    if profile == "dense_geometry":
        # Bias toward smaller set of bays to intensify spatial pressure.
        preferred = (idx // 3) % max(2, m - 1)
        raw = [15.0 + rng.random() * 25.0 for _ in range(m)]
        raw[preferred] += 45.0
        return normalize_to_100(raw)

    if profile == "crane_trap":
        preferred = (idx // 5) % m
        raw = [18.0 + rng.random() * 20.0 for _ in range(m)]
        raw[preferred] += 35.0
        return normalize_to_100(raw)

    # balanced / tight_due
    preferred = (idx + rng.randrange(m)) % m
    raw = [20.0 + rng.random() * 35.0 for _ in range(m)]
    raw[preferred] += 25.0
    return normalize_to_100(raw)


def make_release_time(idx: int, n_blocks: int, n_bays: int, rng: random.Random, profile: str) -> int:
    cfg = PROFILE_CONFIG[profile]
    window = max(8, int((n_blocks / max(1, n_bays)) * cfg["release_window_factor"]))
    batch = int(cfg["release_batch"])

    if profile in {"dense_geometry", "crane_trap"}:
        # Batch releases create many simultaneous candidates and occupancy conflicts.
        return (idx // batch) % window

    if profile == "tight_due":
        return (idx * 2 + rng.randrange(0, 4)) % window

    return (idx * 3 + rng.randrange(0, 6)) % window


def make_processing_time(src_proc: int, rng: random.Random, profile: str) -> int:
    lo, hi = PROFILE_CONFIG[profile]["proc_delta"]
    return max(2, int(src_proc) + rng.randint(lo, hi))


def make_due_date(release: int, proc: int, rng: random.Random, profile: str) -> int:
    lo, hi = PROFILE_CONFIG[profile]["slack_range"]
    return release + proc + rng.randint(lo, hi)


def make_workload(src_workload: int, idx: int, rng: random.Random, profile: str) -> int:
    if profile == "workload_balance":
        # Wider workload spread stresses Z2.
        return max(5, int(src_workload * rng.uniform(0.6, 1.9)) + (idx % 7))
    if profile == "preference_skew":
        return max(5, int(src_workload * rng.uniform(0.8, 1.5)))
    return max(5, int(src_workload * rng.uniform(0.75, 1.45)))


def make_block(src_block: dict, idx: int, n_blocks: int, bays: list[dict], rng: random.Random, profile: str) -> dict:
    cfg = PROFILE_CONFIG[profile]
    scale_min, scale_max = cfg["scale_range"]

    # Deterministic variation with random jitter from a fixed seed.
    scale = rng.uniform(scale_min, scale_max)
    shape = safe_scaled_shape(src_block["shape"], scale, bays)

    release = make_release_time(idx, n_blocks, len(bays), rng, profile)
    proc = make_processing_time(int(src_block["processing_time"]), rng, profile)
    due = make_due_date(release, proc, rng, profile)

    return {
        "release_time": release,
        "due_date": due,
        "processing_time": proc,
        "workload": make_workload(int(src_block["workload"]), idx, rng, profile),
        "bay_preferences": make_preferences(len(bays), idx, rng, profile),
        "shape": shape,
    }


def generate_case(base: dict, case: BenchmarkCase) -> dict:
    if case.profile not in PROFILE_CONFIG:
        raise ValueError(f"Unknown profile: {case.profile}")

    rng = random.Random(case.seed)
    bays = make_bays(case.bays, case.profile)
    base_blocks = base["blocks"]

    # Reorder source blocks per case so repeated geometry does not follow the same pattern.
    source_order = list(range(len(base_blocks)))
    rng.shuffle(source_order)

    blocks: list[dict] = []
    for idx in range(case.blocks):
        src_idx = source_order[idx % len(source_order)]
        src_block = base_blocks[src_idx]
        blocks.append(make_block(src_block, idx, case.blocks, bays, rng, case.profile))

    return {
        "name": case.name,
        "bays": bays,
        "blocks": blocks,
        "weights": copy.deepcopy(PROFILE_CONFIG[case.profile]["weights"]),
    }


def summarize_problem(problem: dict) -> str:
    releases = [b["release_time"] for b in problem["blocks"]]
    dues = [b["due_date"] for b in problem["blocks"]]
    procs = [b["processing_time"] for b in problem["blocks"]]
    workloads = [b["workload"] for b in problem["blocks"]]
    return (
        f"{problem['name']}: bays={len(problem['bays'])}, blocks={len(problem['blocks'])}, "
        f"release=[{min(releases)},{max(releases)}], "
        f"due=[{min(dues)},{max(dues)}], "
        f"proc_avg={sum(procs)/len(procs):.1f}, "
        f"workload_avg={sum(workloads)/len(workloads):.1f}"
    )


def write_problem(problem: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{problem['name']}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(problem, f, indent=2)
        f.write("\n")
    return path


def load_base() -> dict:
    with BASE_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["default", "smoke"], default="default")
    parser.add_argument("--single", action="store_true", help="Generate one custom case instead of a predefined suite.")
    parser.add_argument("--name", default="custom_B5_b120_dense_geometry")
    parser.add_argument("--bays", type=int, default=5)
    parser.add_argument("--blocks", type=int, default=120)
    parser.add_argument("--profile", choices=sorted(PROFILE_CONFIG), default="dense_geometry")
    parser.add_argument("--seed", type=int, default=202699)
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    base = load_base()
    output_dir = Path(args.output_dir)

    if args.single:
        cases = [BenchmarkCase(args.name, args.bays, args.blocks, args.profile, args.seed)]
    else:
        cases = SMOKE_SUITE if args.suite == "smoke" else DEFAULT_SUITE

    for case in cases:
        problem = generate_case(base, case)
        path = write_problem(problem, output_dir)
        print(f"Generated {path}")
        print("  " + summarize_problem(problem))


if __name__ == "__main__":
    main()
