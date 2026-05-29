"""
Generate larger OGC 2026 COCO example instances from the official small example.

Usage:
    python alg_tester/example/generate_large_example.py
    python alg_tester/example/generate_large_example.py --n-blocks 40 --profile tight

This script preserves real block geometry from example_B2_b10.json and only varies
scheduling metadata, workload, and bay preferences. That makes the generated
instances useful for testing algorithm scalability without inventing invalid shapes.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


BASE_FILE = Path(__file__).with_name("example_B2_b10.json")


def make_preferences(src_pref: list[int], idx: int, profile: str) -> list[int]:
    """Return 2-bay preferences summing to 100."""
    if profile == "preference_skew":
        # Strongly alternate preferred bay to test assignment trade-offs.
        return [85, 15] if idx % 2 == 0 else [15, 85]

    # Mild deterministic perturbation around the source preference.
    p0 = src_pref[0]
    delta = ((idx * 17) % 31) - 15
    p0 = max(10, min(90, p0 + delta))
    return [p0, 100 - p0]


def make_block(src_block: dict, idx: int, profile: str) -> dict:
    block = copy.deepcopy(src_block)

    # Deterministic but varied metadata.
    if profile == "tight":
        release = (idx * 2) % 18
        proc = max(3, int(src_block["processing_time"] + (idx % 3) - 1))
        slack = 1 + (idx % 4)
    elif profile == "wide":
        release = (idx * 3) % 30
        proc = max(3, int(src_block["processing_time"] + (idx % 4) - 1))
        slack = 8 + (idx % 8)
    elif profile == "crane_pressure":
        # Many blocks overlap in time, increasing spatial/crane pressure.
        release = idx % 6
        proc = max(4, int(src_block["processing_time"] + (idx % 2)))
        slack = 2 + (idx % 3)
    else:
        release = (idx * 2) % 24
        proc = max(3, int(src_block["processing_time"] + (idx % 3) - 1))
        slack = 4 + (idx % 6)

    block["release_time"] = release
    block["processing_time"] = proc
    block["due_date"] = release + proc + slack
    block["workload"] = max(5, int(src_block["workload"] + ((idx * 11) % 21) - 10))
    block["bay_preferences"] = make_preferences(src_block["bay_preferences"], idx, profile)

    return block


def generate(n_blocks: int, profile: str) -> dict:
    with BASE_FILE.open("r", encoding="utf-8") as f:
        base = json.load(f)

    base_blocks = base["blocks"]
    blocks = []
    for idx in range(n_blocks):
        src = base_blocks[idx % len(base_blocks)]
        blocks.append(make_block(src, idx, profile))

    problem = {
        "name": f"example_B2_b{n_blocks}_{profile}",
        "bays": base["bays"],
        "blocks": blocks,
        "weights": {
            # Keep tardiness important, but not so dominant that preference/workload are invisible.
            "w1": 2667,
            "w2": 7,
            "w3": 20,
        },
    }
    return problem


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-blocks", type=int, default=30)
    parser.add_argument(
        "--profile",
        choices=["balanced", "tight", "wide", "crane_pressure", "preference_skew"],
        default="crane_pressure",
    )
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    problem = generate(args.n_blocks, args.profile)
    output = Path(args.output) if args.output else Path(__file__).with_name(
        f"example_B2_b{args.n_blocks}_{args.profile}.json"
    )
    with output.open("w", encoding="utf-8") as f:
        json.dump(problem, f, indent=2)
        f.write("\n")

    print(f"Generated: {output}")
    print(f"name={problem['name']}, bays={len(problem['bays'])}, blocks={len(problem['blocks'])}")


if __name__ == "__main__":
    main()
