# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OGC 2026 (Optimization Grand Challenge) — a shipyard block-stowage scheduling problem. Solvers assign blocks to rectangular bays with `(x, y, orient, entry_time, exit_time)` decisions, then a feasibility checker validates the solution and computes a weighted objective `w1*tardiness + w2*load_imbalance + w3*bay_preference_penalty`.

## Environment

```bash
conda env create -f ogc2026_env.yml
conda activate ogc2026
```

Python 3.12, PyQt6 GUI, Shapely for geometry, plus heavy optional deps (Gurobi, Xpress, OR-Tools, Torch, TF). Numba and OpenJDK are also pulled in.

## Common Commands

```bash
# Launch the GUI tester (browse instance + algorithm folder, click Run)
conda activate ogc2026
cd alg_tester && python alg_tester_app.py

# Headless batch evaluation: runs baseline_greedy vs myalgorithm on every
# benchmark JSON and prints a comparison table.
python evaluate_all.py --timelimit 60 --greedy-timelimit 10
python evaluate_all.py --pattern "smoke_*.json" --output results.json

# Generate benchmark suite (writes into alg_tester/example/benchmark/)
python alg_tester/example/generate_benchmark_suite.py --suite smoke
python alg_tester/example/generate_benchmark_suite.py --single --name my_dense \
    --bays 5 --blocks 120 --profile dense_geometry
```

There is no test framework, lint config, or build step — evaluation against benchmark instances *is* the test loop.

## Architecture

### The contestant contract

`baseline/myalgorithm.py` exports a single function `algorithm(prob_info: dict, timelimit: float) -> dict`. **Do not change this signature.** Everything else in `baseline/` is editable; `utils.py` is the official scoring code and should not be modified.

The solution dict format is fully documented at the top of [baseline/baseline_greedy.py](baseline/baseline_greedy.py): a flat `operations` dict keyed by integer time-as-string, with EXIT ops preceding ENTRY ops at each timepoint. ENTRY carries `(bay_id, x, y, orient_idx)`; EXIT just carries `(bay_id, block_id)`.

### The physical model (read [alg_tester/utils.py](alg_tester/utils.py) header)

- Bays are integer-grid rectangles. Blocks are *multi-layer* polygons (layer 0 is the lowest physical level).
- The crane only moves vertically, so collision checking uses the **`j >= k` descent-path rule**: when a new block's layer `k` descends, it sweeps through the heights of all existing-block layers `j >= k`. This is why `check_entry`/`check_exit`/`check_collisions` are not trivial AABB tests — they require per-layer Shapely intersection in 3D.
- `check_feasibility` runs 5 ordered stages: (1) assignment validity, (2) crane entry, (3) crane exit, (4) spatial collisions + boundary, (5) sequential operation replay. The returned `stage` is the *earliest* failing stage, not necessarily the only one.

### Duplicate utils.py — important

`baseline/utils.py` and `alg_tester/utils.py` are byte-identical copies. The tester imports its own; `myalgorithm.py` and `baseline_greedy.py` import the one in `baseline/`. If you ever edit `utils.py`, keep both in sync — but the README explicitly says contestants must NOT modify it.

### Baseline greedy structure

[baseline/baseline_greedy.py](baseline/baseline_greedy.py) is the reference solver and is heavily reused by `myalgorithm.py`. Key internal entry points (private but called from outside):

- `_place_blocks(...)` — shared placement kernel (used by Phase 1 and the repair loop).
- `_find_earliest_slot(new_blk, bay, placed_in_bay, schedule_in_bay, r_time, proc)` — the crane-feasible time-slot search. **`myalgorithm.py` monkey-patches this to inject extra checks.**
- `_repair(...)` — iterative re-placement of blocks failing feasibility; supports `"greedy"` and `"simple"` modes.
- `_build_operations(assignments)` — converts internal `(bay, x, y, orient, entry, exit)` tuples into the official `operations` dict.

### myalgorithm.py strategy (the current Hermes solver)

The current `algorithm()` in [baseline/myalgorithm.py](baseline/myalgorithm.py) does several non-obvious things future Claude instances need to know:

1. **OBB precompute + cache** (`precompute_obbs`, `obb_cache`) — keyed by `(block_id, orient_idx)`, storing each shape's minimum rotated rectangle in *local* coords. `get_world_obb(block)` translates by `(block.x, block.y)` lazily and memoises on `block._world_obb`.
2. **Monkey-patches `utils.check_entry/check_exit/check_collisions` and `baseline_greedy._find_earliest_slot`** at the top of `algorithm()` with custom 3-stage filters (AABB → OBB → full Shapely). The originals are captured at module import time into `original_check_entry` etc. and **restored before returning** — so the patches don't leak into `evaluate_all.py`'s next iteration. If you add new patches, restore them too.
3. **Portfolio of 12 named permutation heuristics** (`EDD`, `MST`, `ERD`, `LPT`, `SPT`, `LargestArea`, `Midpoint`, `SlackRatio`, `SlackComb_*`) — but only `EDD` and `SlackRatio` are currently evaluated as initial seeds (`target_heuristics`), to leave more wall-clock for SA.
4. **Simulated Annealing over permutations** with `swap`/`insert`/`invert` moves, 50% probability of focusing on tight-slack blocks, reheat at `T < 0.01`, 90% of `timelimit` budget. Evaluating one neighbor means running the full greedy `_place_blocks` + `_repair` pipeline — this is expensive, so the OBB caching matters.
5. `evaluate_permutation` calls `_place_blocks` then `_repair`, which both expect the patched `_find_earliest_slot`. `evaluate_all.py` defensively re-resets the patches before each `greedyalgorithm` call (see lines 55–58) so the baseline runs unpatched.

### Benchmark instances

`alg_tester/example/` contains:
- `example_B2_b10.json` — the tiny seed instance (2 bays, 10 blocks).
- `generate_large_example.py` and `generate_benchmark_suite.py` — instance generators with profiles like `balanced`, `tight_due`, `dense_geometry`, `crane_trap`, `preference_skew`, `workload_balance`.
- `benchmark/` — the generated suite that `evaluate_all.py` iterates over.

`B<n>` in filenames = number of bays; `b<n>` = number of blocks. `smoke_*` are fast sanity instances; `bench_*` and `my_B5_b200_hard.json` are the harder runs.
