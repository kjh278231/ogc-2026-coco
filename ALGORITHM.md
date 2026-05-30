# Hermes Solver — Algorithm Reference

This document describes the current state of `baseline/myalgorithm.py` (the
"Hermes" solver) end-to-end: pipeline phases, data structures, monkey-patches,
heuristic portfolio, simulated annealing loop, time budgeting, and the
interaction with `baseline/baseline_greedy.py`.

> **Maintenance rule.** Whenever you modify `baseline/myalgorithm.py` or
> `baseline/baseline_greedy.py` in a way that changes any of the items below
> (pipeline order, cache shape, monkey-patch surface, heuristic set, SA move
> distribution, budget formulae, event schema, deadline propagation), update
> the matching section of this file in the **same commit**. See the
> "Maintenance rule" section at the end for the trigger checklist.

---

## 1. One-paragraph summary

Hermes is a portfolio + simulated-annealing metaheuristic for the OGC2026
block-stowage problem. It precomputes per-orientation OBBs and per-layer
Shapely polygons in local coordinates, monkey-patches `utils.check_entry /
check_exit / check_collisions` with 3-stage hierarchical filters (AABB →
OBB → full polygon) and replaces `baseline_greedy._find_earliest_slot`
with a crane-aware variant that also rejects placements that would block a
future block's exit. It then evaluates four named priority heuristics (EDD,
SlackRatio, MST, LargestArea) under a per-seed time cap, picks the best
feasible seed, falls back directly to forced-placement when all seeds fail
(post H-001), and runs a swap/insert/invert SA loop over block permutations
under a 90% wall-clock budget with per-iteration repair cap. All patches are
restored before `algorithm()` returns. Structured JSONL events are emitted
when `OGC2026_EVENT_LOG` is set, so `tools/eval_runner.py` can capture
fine-grained traces per (run, instance).

---

## 2. Top-level pipeline (`algorithm(prob_info, timelimit=60)`)

| Phase | Lines | Purpose |
|---|---|---|
| 0 | 383–387 | Record `start_time`, init event log, derive `hard_deadline` and `safety_margin = clamp(0.05, timelimit*0.02, 0.5)` |
| 1 | 396 | `precompute_obbs(prob_info)` — populate `obb_cache` and `local_polys_cache` |
| 2 | 399–402 | Monkey-patch `utils.check_entry/exit/collisions` and `baseline_greedy._find_earliest_slot` |
| 3 | 414 | Build `Bay` objects from `prob_info["bays"]` |
| 4 | 417–487 | Define the 12-heuristic portfolio (computed eagerly even though only 4 are evaluated — cheap sort calls) |
| 5 | 489–563 | Define `evaluate_permutation` and `evaluate_forced_permutation` closures |
| 6 | 570–622 | Init selection: evaluate `target_heuristics`, fall back to `forced_direct` (H-001) if all seeds infeasible |
| 7 | 626–728 | Simulated Annealing loop over permutations until `search_deadline` |
| 8 | 730–740 | Restore originals, emit `algo.end`, close event log, return `best_sol` |

---

## 3. Data structures and caches

### 3.1 `obb_cache : dict[(block_id, orient_idx) -> Shapely Polygon]`

Local minimum-rotated-rectangle for the union of all layer vertices, anchored
so that `layers[0][0]` lands at `(0, 0)`. World OBB obtained by Shapely
`translate(local, block.x, block.y)`. Memoised on the `Block` instance via
`block._world_obb` so each `Block` pays the translate cost at most once.

### 3.2 `local_polys_cache : dict[(block_id, orient_idx) -> list[Polygon | None]]`

One Shapely polygon per *layer*, in local coordinates. World polys obtained
by per-layer `translate(p, bx, by)`. Memoised on `block._world_polys`. This
replaces the previous per-`Block`-instance Shapely reconstruction inside
`check_entry/exit/collisions` and is the main reason SA can afford 9–20
iterations on n=120+ instances in 30 seconds.

### 3.3 Anchor convention

Both caches are anchored to `layers[0][0]` of the orientation. Final
`(block.x, block.y)` is therefore the world location of `layers[0][0]`,
**not** of the OBB centroid or the AABB corner.

---

## 4. Monkey-patches

All patches are installed at the top of `algorithm()` (lines 399–402) and
restored at the bottom (lines 730–733). Originals are captured into
module-level `original_*` variables at *import time* so the patches never
self-reference.

### 4.1 `custom_check_entry` / `custom_check_exit` (3-stage filter)

```
Stage 1: AABB overlap        (utils._bb_overlap, O(1))
Stage 2: OBB overlap         (Shapely intersects on cached OBBs)
Stage 3: Per-layer polygon intersection with j >= k descent rule
```

Only triple-stage failures produce an `EntryObstruction`. The first two
stages are cheap and prune the vast majority of pairs on dense bays. If the
block fails the boundary check, falls through to the original
`utils.check_entry` so the official boundary-violation sentinel is preserved.

Honors `baseline_greedy._active_deadline_reached()` after every existing
block — returns `[None]` to signal "deadline hit, don't trust this result".

### 4.2 `custom_check_collisions`

Same 3-stage filter applied pairwise within a bay. Returns the standard
`CollisionResult` records the official scorer expects.

### 4.3 `custom_find_earliest_slot`

Replaces `baseline_greedy._find_earliest_slot`. Adds **Future EXIT
Blocking Prevention**: for every already-placed block `b_other` whose exit
time falls inside `[entry, exit_t)`, runs `check_exit(bay, [new_blk],
b_other, fast=True)` — if `new_blk` would block `b_other`'s exit, the
candidate `entry` is rejected. This is the only place in the codebase that
proactively guards against "we'll regret placing this block here when X
needs to leave".

Also rejects on:
- Stage-2 entry obstruction (`check_entry` with co-present blocks)
- Stage-3 exit obstruction (`check_exit`)
- Stage-4 interior collision (`check_collisions` with blocks strictly
  contained in `[entry, exit_t)`)

Deadline-aware throughout via `_active_deadline_reached()`.

---

## 5. Initial heuristic portfolio

12 heuristics are computed eagerly (lines 427–487). The actual evaluation
list is **`target_heuristics = ["EDD", "SlackRatio", "MST", "LargestArea"]`**
(line 570).

| Name | Sort key | Idea |
|---|---|---|
| `EDD` | `(due_date, processing_time)` | Earliest-due-date — classic deadline heuristic |
| `MST` | `(slack, due_date)` where `slack = due - release - proc` | Minimum-slack-time-first |
| `ERD` | `(release_time, due_date)` | Earliest-release-date |
| `LPT` | `(-processing_time, due_date)` | Longest-processing-time-first |
| `SPT` | `(processing_time, due_date)` | Shortest-processing-time-first |
| `LargestArea` | `(-area, due_date)` | Geometry-first; wins on dense_geometry / crane_trap profiles |
| `Midpoint` | `(release + due, due)` | Schedule-window midpoint |
| `SlackRatio` | `((due - release) / max(1, proc), due)` | Relative slack |
| `SlackComb_Balanced` | `eval_priority_score(1, 1, 0.5)` | `α·D + β·slack − γ·P`, balanced |
| `SlackComb_SlackHeavy` | `eval_priority_score(0.2, 1, 0.1)` | Slack-dominated |
| `SlackComb_LPT_Heavy` | `eval_priority_score(1, 0.5, 1)` | Penalizes long jobs |
| `SlackComb_HighPriority` | `eval_priority_score(0.5, 1, 1)` | Mid-balance |

Per-seed budget: `init_check_limit = max(0.5, timelimit * 0.30 / len(target_heuristics))`.
At `timelimit=30s` and 4 seeds, that's **2.25s per seed**.

---

## 6. `evaluate_permutation` and `evaluate_forced_permutation`

### 6.1 `evaluate_permutation(perm, search_deadline)`

1. Bail with `make_timeout_result()` if `time.time() >= search_deadline`.
2. Run `baseline_greedy._place_blocks(perm, …, deadline=search_deadline)`.
3. Build solution dict via `_build_operations`.
4. If deadline hit and remaining time is too thin to absorb `check_feasibility`'s
   Shapely cost (`> max(2.0, safety_margin*4)`), return the raw greedy solution
   with a timeout result — protects against the "init looks infeasible only
   because the scorer was cut" pathology.
5. Otherwise run `_repair(repair_mode="greedy", deadline=search_deadline)`.
6. Re-build operations dict and return `(check_feasibility(...), sol)`.

### 6.2 `evaluate_forced_permutation(perm, search_deadline)`

Skips repair. Passes `forced_ids=set(perm)` to `_place_blocks`, which routes
every block through the `_empty_bay_entry` path. Structurally guaranteed
feasible per the `_force_place` docstring (as long as enough time remains).

---

## 7. Initial selection & fallback (post H-001)

```
for name in ["EDD", "SlackRatio", "MST", "LargestArea"]:
    if no_time_left: emit init.skipped; break
    perm = heuristics[name]
    res, sol = evaluate_permutation(perm, deadline = now + init_check_limit)
    emit init.heuristic_result(name, feasible, stage, objective, wall_time)
    if res.feasible and res.objective < best_obj:
        keep as best

if best_perm is None:                            # all seeds infeasible
    # H-001 (4972d5f): skip the old edd_retry step that burned ~20s
    # without progress; route directly to forced placement.
    emit init.fallback(path="forced_direct", reason="all_seeds_infeasible")
    forced_res, forced_sol = evaluate_forced_permutation(EDD, hard_deadline)
    best = forced if feasible else {"operations": {}}
    emit init.fallback.outcome(path="forced_direct", ...)
else:
    emit init.chosen(name, objective)
```

The `silence_stdout()` context manager wraps the loop to suppress greedy's
print spam.

---

## 8. Simulated annealing loop

### 8.1 Setup (lines 626–650)

| Variable | Value | Purpose |
|---|---|---|
| `search_deadline` | `min(hard_deadline - safety_margin, start_time + timelimit*0.90)` | Hard upper bound on SA wall-clock |
| `per_iter_repair_cap` | `max(2.0, timelimit*0.05)` | Per-neighbor budget for `_place_blocks + _repair`; prevents one bad neighbor from eating the rest of SA |
| `tight_blocks` | Top `max(3, n//3)` by ascending raw slack `D - R - P` | Limited-Local-Search focus set, precomputed before the loop |
| `T` | 100.0 | Initial temperature |
| `cooling_rate` | 0.97 | Per-iteration multiplicative cooling |
| Reheat threshold | `T < 0.01` → `T = 100.0` | Escape local minima |

### 8.2 Move generation

```
move_type = random.choice(["swap", "insert", "invert"])

if tight_blocks is not None and random.random() < 0.50:
    idx1 = position of a random tight block
    idx2 = random
else:
    idx1, idx2 = random, random

apply move
```

50% of iterations focus one index on a tight-slack block; the other half is
fully random.

### 8.3 Acceptance

Standard Metropolis criterion: if `obj < curr_obj`, accept and update best.
Otherwise accept with probability `exp(-(obj - curr_obj) / max(1.0, T))`.

### 8.4 Event emission

- `sa.improvement` on every new best (with `iteration`, `move_type`, `objective`)
- `sa.complete` at loop exit (with `iterations`, `improvements`, `best_objective`)

Note: rejected and equal-objective iterations are **not** logged — the SA
iteration count comes from the `sa.complete` event only.

---

## 9. Time budget structure

```
[start_time, start_time + timelimit]            ← hard deadline window
            ├─ safety_margin = clamp(0.05, timelimit*0.02, 0.5)
            ├─ 30% init phase total = init_check_limit * |target_heuristics|
            ├─ 90% search_deadline (SA cutoff)
            └─ per-iter cap = max(2.0, timelimit*0.05)
```

At `timelimit=30`:
- `safety_margin = 0.5`
- `init_check_limit = 2.25` per seed → 9s total if all 4 used
- `search_deadline = min(29.5, 27.0) = 27.0`
- `per_iter_repair_cap = 2.0`

`hard_deadline` and `search_deadline` are propagated into
`baseline_greedy._place_blocks` and `_repair` via the `deadline=` kwarg.

---

## 10. Event log schema

Set `OGC2026_EVENT_LOG=path.jsonl` before invoking `algorithm()` to record
events. The `_emit(event, **payload)` helper appends one JSON line per call.

| Event | Fired when | Payload |
|---|---|---|
| `algo.start` | top of `algorithm()` | `timelimit` |
| `algo.context` | after monkey-patches | `n_blocks, n_bays, w1, w2, w3` |
| `init.start` | before portfolio loop | `target_heuristics, init_check_limit` |
| `init.heuristic_result` | after each seed eval | `name, feasible, stage, objective, wall_time` |
| `init.skipped` | when remaining time too low | `name, reason` |
| `init.chosen` | best seed picked | `name, objective` |
| `init.fallback` | all seeds infeasible (H-001) | `path="forced_direct", reason` |
| `init.fallback.outcome` | after forced placement | `path, feasible, objective` |
| `sa.improvement` | new best in SA | `iteration, move_type, objective` |
| `sa.complete` | SA loop exit | `iterations, improvements, best_objective` |
| `algo.end` | before return | `best_objective, wall_time, has_solution` |

`t` is wall-clock seconds since `_init_event_log(t0)` was called.

---

## 11. `baseline_greedy` interactions

The Hermes solver depends on private internals of `baseline_greedy`:

| Symbol | Used for | Coupling risk |
|---|---|---|
| `_place_blocks(perm, …, deadline=)` | Core placement kernel in both init and SA | High — signature changes break SA |
| `_repair(prob_info, sol, assignments, …, deadline=)` | Greedy repair pass | High |
| `_build_operations(list_of_assignments)` | Convert internal tuples → solution dict | High |
| `_find_earliest_slot` | **Monkey-patched** by `custom_find_earliest_slot` | Critical |
| `_active_deadline_reached()` | Cooperative deadline check inside patches | Required for time-budget honesty |
| `_time_overlaps(a1, e1, a2, e2)` | Half-open interval overlap | Low |
| `_empty_bay_entry` | Forced fallback path | Stage-2/3 guarantee depends on this |

All four `baseline_greedy._*` deadline parameters were added together with
H-001's scaffolding (commit `4972d5f`). Removing the deadline kwarg would
break Hermes's wall-clock honesty.

---

## 12. Known limits and open issues

1. **b150 stuck at forced-placement local optimum.** As of run_3, SA's
   swap/insert/invert neighborhood cannot escape the forced solution's basin
   on `bench_B5_b150_mixed_hard`. Candidate fixes:
   - H-002 `repair_mode="simple"` seed before SA (gives a non-degenerate
     start).
   - Coarser neighborhood operator (bay-reassignment, bulk segment swap).

2. **All four portfolio seeds fail stage 2 on dense bench_B5.** Confirmed by
   `tools/geometry_debug.py --probe-edd` to be **placement collision** (not
   crane geometry): blocks placed inside each other's footprints with overlap
   areas of 30–56 grid units. Suggests `custom_find_earliest_slot`'s
   pre-collision check is incomplete on hard instances — its Stage-4
   pre-check only catches blocks strictly interior to `[entry, exit_t)`.

3. **SA per-iteration cost is high (~2–3s on n=120).** Limits exploration to
   ~10 iterations in 30s. The `local_polys_cache` already amortizes Shapely
   reconstruction; further gains would require a partial-resimulation rather
   than full `_place_blocks` re-run per neighbor.

4. **Reheat at `T < 0.01` may fire too often on short runs.** With
   `cooling_rate = 0.97`, `T` reaches 0.01 after ~300 iterations. Not
   currently reached on bench_B5 due to per-iter cost.

5. **Move uniformity.** `random.choice(["swap", "insert", "invert"])` weights
   all moves equally; no empirical justification recorded.

---

## 13. Maintenance rule

**This document and `baseline/myalgorithm.py` / `baseline/baseline_greedy.py`
must stay in sync.** Trigger an `ALGORITHM.md` update in the **same commit**
as the code change whenever you do any of the following:

| Change in code | Section to update |
|---|---|
| Add/remove a phase in `algorithm()` | §2 Top-level pipeline |
| Add/remove a cache, change anchor | §3 Data structures and caches |
| Add/remove/alter a monkey-patch surface | §4 Monkey-patches |
| Add/remove a heuristic, change `target_heuristics` | §5 Initial heuristic portfolio |
| Change `evaluate_permutation` / forced variant flow | §6 |
| Change init-selection or fallback path | §7 |
| Change SA setup, move set, acceptance, or schedule | §8 |
| Change any budget formula | §9 Time budget structure |
| Add/remove an emit call or rename event | §10 Event log schema |
| Change deadline propagation or `baseline_greedy` private symbols used | §11 |
| Resolve or add a known limit | §12 Known limits and open issues |

For trivial changes (comment edits, formatting, log-message wording), no doc
update is required.

The `solver-developer` agent is wired to honour this rule automatically when
implementing hypotheses; see `.claude/agents/solver-developer.md`. The
broader CLAUDE.md / AGENTS.md project rules also surface it for any other
session that touches the solver.
