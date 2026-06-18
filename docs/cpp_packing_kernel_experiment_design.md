# C++ Packing Kernel Experiment Design

> Created 2026-06-17. Goal: test whether moving only the packing hot kernels from
> Python/Numba to a precompiled native implementation removes Numba cold-start cost and
> improves wall-clock performance without changing search behavior.
>
> **Update 2026-06-17 (review + organizer confirmation).**
> - **Permission RESOLVED = YES.** Organizer (Discord, 6/2): compiled non-Python
>   submissions are allowed, and may dynamically link the server Gurobi runtime
>   *linked to exactly gurobi 13.0.2* (`libgurobi130.so`). So native binaries are
>   permitted -- the old "is it allowed" showstopper is closed.
> - **Remaining gate 0 (two parts, BOTH must pass before investing):**
>   1. **Build-environment match.** We build the binary ("your compiled code"), so it
>      must load on the eval server: **Linux x86_64**, matching glibc, gurobi 13.0.2,
>      and (if a CPython extension) the eval's CPython/numba ABI. We develop on Windows
>      -> need a Linux build pipeline. **DECISION: deferred until a WSL Linux env is set
>      up (user will configure); do the conversion then.**
>   2. **Cold-start size (the value question, unchanged).** Numba is already native at
>      steady state, so C++ per-call speed ~= numba; the ONLY win is removing JIT
>      cold-start. If cold-start is small, C++ yields ~nothing regardless of permission.
>      **Measure first (Section 9), now amplified by the parallel portfolio: N worker
>      processes spawn -> cold-start x N.** This is runnable on Windows now.
> - **GATE 0 PART 2 MEASURED (2026-06-18) -> REJECT.** Numba cold-start is tiny:
>   warm disk cache (normal per-instance) prob_1/13/20 = **0.16 / 0.31 / ~0s**; cold cache
>   (first contest instance, full JIT compile) prob_13 = **0.63s** — i.e. ~1-3% of the
>   12-23s wall. The ONLY thing C++ removes is this cold-start, so the payoff is ~1-3% for
>   a large Linux build pipeline + OS/packaging risk. **Worse: numba0 (pure Python) ~= numba1
>   in this path** (prob_13 14.5 vs 14.43) -> the numba'd kernels (Targets A/B/C) are NOT the
>   bottleneck; shapely mask rasterization (~39%, not numba'd) is. So the only C++ target with
>   value is the float-geometry rasterizer (Target D), which is the hardest/safety-critical.
>   **Verdict: do not pursue the C++ kernel port for leaderboard performance.** (`.claude/scratch`
>   coldstart measurement; design doc Section 9.) The novelty angle for the report remains, but
>   performance ROI is poor. Also note (separate finding): numba's value in the current mask-only
>   path is worth re-checking — it may be droppable.
> - **Cheaper sibling unlocked: Gurobi from Python (no C++).** gurobipy 13.0.2 is also
>   installed, so OR-Tools -> Gurobi for the set-partitioning recombine can be A/B-tested
>   in pure Python (no native build). Our recombine is pool-bound (capped to solve in 8s,
>   SOLVER_POOL_PER_BAY); Gurobi may solve larger pools faster -> relax the cap. Try this
>   BEFORE the C++ port -- but note gurobipy is NOT in the local .venv, so it also waits
>   for the WSL/eval env. See Section 11b.

## 0. Purpose

The current solver is mostly Python orchestration around a per-bay admission packer.
Recent incremental-eval profiling showed candidate evaluation is **packing-bound**:

- removable objective bookkeeping: 6.39%
- `solve_bay` / packing: 93.61%

So a whole-solver C++ rewrite is the wrong first move. The experiment should target only
the kernels under `solve_bay`:

- `find_slot`
- `find_slot_mask`
- `masks_overlap`
- optionally `_local_mask` rasterization, after the simpler kernels pass

The intended payoff is twofold:

1. keep the Numba speedup without paying JIT cold-start cost;
2. make per-instance wall time lower at the same search behavior.

## 1. Non-Goal

Do **not** port the whole solver to C++ in the first experiment.

Keep Python responsible for:

- search control flow;
- assignment/objective logic;
- recombination and OR-Tools integration;
- final guard/build orchestration;
- fallback behavior.

The C++ boundary should be narrow and testable: input arrays in, first feasible slot or
overlap boolean out.

## 2. Hypothesis

A precompiled native packing kernel will be behavior-identical to the current Python/Numba
kernel at fixed eval count and will lower wall time, especially in 60s one-shot runs where
Numba JIT/cache warmup is paid per process.

Expected impact:

- fixed eval count: objective must be identical;
- fixed eval count: wall should improve versus current `SOLVER_NUMBA=1`;
- fixed wall: objective may improve through more evaluations, but only after the fixed-eval
  gate passes.

## 3. Why Not Full C++ Now

A full rewrite touches too much at once:

- Python data structures and solver state;
- Shapely/mask geometry assumptions;
- OR-Tools recombination;
- time-budget fallback behavior;
- final true-objective guard.

That makes regressions hard to localize. A kernel port gives the useful speed test while
keeping the existing algorithm and safety machinery intact.

## 4. Candidate Native Targets

### Target A -- `masks_overlap`

Current role:

- compares two uint64-packed supercover masks row-by-row;
- already has a Numba version (`_masks_overlap_u64`);
- pure, small, easy to validate exhaustively.

Why first:

- lowest correctness surface;
- no geometry dependency;
- mismatch test can generate many random aligned masks.

Gate:

- 0 mismatches versus Python/Numba implementation over random and real mask pairs.

### Target B -- `find_slot` / AABB scan

Current role:

- scans `(x, y)` candidates in row-major order;
- returns the first bottom-left slot whose AABB overlaps no active block;
- already has a Numba `_aabb_scan`.

Gate:

- first returned `(x, y)` must match current implementation exactly;
- no objective mismatch at fixed eval count.

### Target C -- `find_slot_mask`

Current role:

- row-major slot search with AABB prefilter plus mask overlap test;
- likely the best candidate for replacing Numba cold-start with native code.

Gate:

- first returned `(x, y, orient)` must match current implementation exactly;
- no false-negative collision relative to existing mask logic;
- fixed eval objective bit-identical.

### Target D -- `_local_mask` rasterization

Current role:

- builds the conservative supercover mask from Shapely geometry;
- previously identified as a major cost center.

Why later:

- safety-critical: mask-disjoint must imply polygon-disjoint;
- hardest to keep bit-identical because it depends on geometric buffering and point tests.

Possible variants:

- keep Shapely footprint construction, but move point-grid raster scan to native code;
- or implement a separate conservative polygon rasterizer, validated by false-negative tests.

Gate:

- 0 false negatives versus polygon collision tests;
- 0 mismatches versus current mask for a first adoption path, or a separately documented
  conservative-mask proof if not bit-identical.

## 5. Integration Options

### Option 1 -- CPython extension (`.pyd`)

Pros:

- fastest call path;
- no subprocess overhead;
- clean replacement for Numba functions.

Risks:

- Python ABI / platform coupling;
- packaging must match evaluator environment exactly;
- compiled artifacts in submission may be disallowed.

### Option 2 -- CFFI / ctypes DLL

Pros:

- simpler C ABI;
- Python version coupling is lower than CPython extension.

Risks:

- DLL loading and path handling in the evaluator;
- data marshaling overhead can erase gains for small calls.

### Option 3 -- standalone C++ helper process

Pros:

- easiest binary boundary.

Risks:

- subprocess overhead is too high for per-candidate packing calls;
- time management and failure handling get harder.

Recommendation: start with a CPython extension or CFFI DLL only if submission rules permit
native binaries. Otherwise, do not pursue this before the contest submission.

## 6. Correctness Gates

This is a pure speed experiment. Behavior must not change.

Required gates:

1. Unit equivalence:
   - `masks_overlap`: exact boolean match;
   - `find_slot`: exact first slot match;
   - `find_slot_mask`: exact first slot/orientation match.
2. Fixed eval A/B:
   - `SOLVER_MAX_EVALS=N`;
   - native off vs on;
   - objective, feasibility, and eval count must match.
3. Pool/trace side effects:
   - no change to `_POOL`, `_EVALS`, `_TRACE` semantics because native code is below
     objective evaluation and search acceptance.
4. Final feasibility:
   - full build must remain feasible on all train instances.

Any objective mismatch at fixed eval count means reject or debug before timing claims.

## 7. Measurement Protocol

Per `docs/methodology.md`:

- primary correctness: fixed eval count;
- cost axis: wall time at that same eval count;
- payoff conversion: fixed wall time only after correctness passes.

Run matrix:

| run | instances | mode | purpose |
|---|---|---|---|
| unit-real | all real mask/box pairs sampled from `prob_13/14/20` | native vs current | exact kernel match |
| bitident-small | `prob_1/13/20` | `SOLVER_MAX_EVALS=500` | quick trajectory gate |
| bitident-full | 20 | `SOLVER_MAX_EVALS=2500` | correctness gate |
| speed-full | 20 | `SOLVER_MAX_EVALS=2500` | wall delta at same work |
| wall-60 | 20 | `T=60` | objective conversion, cold-start included |
| wall-300 | hard subset first, then 20 | `T=300` | long-budget payoff |

Report:

- objective and feasibility;
- evals used;
- wall per instance;
- summed wall and percent delta;
- cold-start time separately if measurable.

## 8. Success Criteria

Adopt only if all are true:

- fixed-eval objective is bit-identical on full20;
- feasibility remains 20/20;
- wall at fixed eval improves materially over current Numba path, including cold start;
- fixed-wall objective is neutral or better;
- native binary packaging is accepted by the submission environment.

Reject if:

- any fixed-eval mismatch occurs and cannot be explained/fixed;
- speed gain is within noise;
- native packaging is not evaluator-safe;
- cold-start is solved but per-call overhead makes wall neutral.

## 9. First Cheap Experiment

Before writing a broad extension, measure Numba cold-start explicitly:

1. one-process cold run: import solver + first `framework_solve`;
2. same-process warm run: second `framework_solve`;
3. `SOLVER_NUMBA=0` run;
4. compare `T=60` and `SOLVER_MAX_EVALS=500` on `prob_1/13/20`.

If cold-start is small relative to wall, native work is less urgent. If cold-start is
several seconds per instance and Numba is otherwise beneficial, proceed with Target A/B.

## 10. Decision

Current recommendation (updated 2026-06-17):

- do not rewrite the whole algorithm in C++;
- native submission is now **confirmed allowed** (organizer), so the blocker is no longer
  permission but **(1) a Linux x86_64 build env matching the eval server [deferred to the
  WSL setup] and (2) whether cold-start is actually large**;
- **before** any native work: run Section 9 cold-start measurement on Windows (single
  process AND N-worker portfolio spawn) -- if cold-start is small, stop here;
- **also try the cheaper Gurobi-from-Python recombine A/B first** (Section 11b) -- no native
  build, directly targets our pool-bound recombine;
- when the WSL Linux env is ready: start with `masks_overlap` and `find_slot` equivalence
  tests (integer domain -> bit-identity feasible), then `find_slot_mask`; defer `_local_mask`
  (float geometry -> bit-identity hard);
- leave objective/search/recombine in Python (port kernels only).
