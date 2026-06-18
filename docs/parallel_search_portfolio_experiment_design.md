# Parallel Search Portfolio Experiment Design

> **Revision 2026-06-17.** Updated after the recombination rework (commit 4d12cc2:
> mask guard + `SOLVER_RECOMB_SOLVE_S=8` + `SOLVER_POOL_PER_BAY=1000` +
> `SOLVER_RECOMB_CAP=10`). Three design consequences, detailed inline below:
> 1. **Workers now keep recombination on** (`SOLVER_CP_WORKERS=1`, pool cap bounds it
>    to ~8s/process). Recombination is load-bearing — it rescues poor high-budget
>    incumbents (prob_20 @E=10000: 1.87M without it vs 618k with it) — so a
>    no-recombine portfolio (the old V1) would regress vs the current default and is
>    no longer the main experiment.
> 2. **Master final scoring uses an adaptive reserve** (probe one build, reserve
>    `build_cost x #candidates`), because a fixed reserve truncates the build past
>    `poly_deadline` and explodes on big instances (the failure mode that produced a
>    prob_5 817k blow-up during the recombine rework).
> 3. **Evaluate at T=300 as well as T=60.** The portfolio's biggest expected win is at
>    long limits, where single-trajectory search drifts into poor basins and spawn/JIT
>    overhead amortizes. The contest does not announce the per-instance timelimit.

## 0. Purpose

The current submission uses only one Python process for almost all assignment
search work. The only explicit 4-worker section is the OR-Tools CP-SAT
recombination step:

```text
solver._recombine() -> cp.parameters.num_search_workers = SOLVER_CP_WORKERS or 4
```

The expensive path during normal search is:

```text
framework_solve()
  -> improved_search() / local_search() / unified ILS
  -> total_obj()
  -> eval_obj1()
  -> solve_bay()
```

That path is mostly Python + numba single-threaded work. This experiment tests
whether the idle cores can be converted into better solution quality by running
multiple independent search trajectories in parallel and selecting the best
candidate by the same final true-score materializer.

Core question:

> Under the same wall-clock limit and a 4-core budget, does a 4-process
> independent search portfolio improve final objective versus the current
> single-process BRIDGE search?

This is not a test of faster CP-SAT. The target is the main assignment search.

## 1. Current Baseline

Current default path:

```text
myalgorithm.algorithm()
  -> solver.framework_solve(prob, timelimit)
```

Default gates set by `submission/myalgorithm.py`:

- `SOLVER_MASK_SEARCH=1`
- `SOLVER_MASK=1`
- `SOLVER_ADAPTIVE_RESERVE=1`
- `SOLVER_NUMBA=1`
- `SOLVER_UNIFIED_ILS=1`
- `SOLVER_UNIFIED_INIT_FRAC=0.6`
- `SOLVER_MASK_PREPARE=1`

Important properties:

- The search cache is process-local.
- The geometry caches are process-local and cleared per solve.
- `local_search`, `_climb`, `_perturb`, and `solve_bay` do not require shared
  state.
- `_POOL` is process-local and is used by recombination.
- `_recombine` already uses CP-SAT workers and should not be multiplied by four
  without reducing `SOLVER_CP_WORKERS`.

Baseline to compare against:

```text
single-process current default
```

## 2. Main Hypothesis

Independent ILS trajectories are complementary enough that best-of-four beats a
single trajectory under the same total wall time.

Why this is plausible:

- The assignment search is local-minimum driven.
- `_perturb()` creates different basins depending on seed and destroy behavior.
- Previous guided-destroy and R-sweep notes showed instance-dependent wins, which
  suggests portfolio diversity can be valuable even when no single mode is safe
  as the default.
- The final true-score guard can compare candidates on the same materialization
  basis, so a bad worker should not hurt except by consuming wall time.

Expected benefit:

```text
quality gain from search diversity > overhead from multiprocessing + duplicate setup
```

## 3. Proposed Architecture

Add an experiment-only portfolio wrapper, not a default replacement at first.

High-level flow:

```text
master process
  1. compute a guaranteed seed / baseline candidate
  2. launch N independent worker processes, N <= 4
  3. each worker runs assignment search only with a distinct profile
  4. each worker returns its best assignment, metadata, and optional pool summary
  5. master true-scores returned assignments with _score_and_pack()
  6. master emits the best materialized solution
```

Initial N:

```text
N = 4
```

Worker output:

```python
{
    "worker_id": int,
    "profile": str,
    "seed": int,
    "assign": dict[int, int],
    "proxy_obj": float | None,
    "elapsed": float,
    "error": str | None,
}
```

Master selection:

```text
for candidate in [baseline] + worker_results:
    final_obj, packed = solver._score_and_pack(prob, candidate.assign, poly_deadline)
choose minimum final_obj
return solver._solution_from_packed(packed)
```

The master should use the exact same final materialization path as the default
submission. The experiment is invalid if candidates are compared by incompatible
proxy scores.

## 4. Worker Scope

Each worker runs the **full search + recombination pipeline** (single-threaded
CP-SAT) and returns its final assignment. It skips only the final solution
materialization, which the master does once for the winner.

Recommended worker behavior:

```text
run seed generation
run improved_search/local_search/unified ILS
run CP-SAT recombination (SOLVER_CP_WORKERS=1) on the worker's own process-local _POOL
do not run final _score_and_pack() for solution emission
return best (post-recombine) assignment only
```

Reason (updated 2026-06-17):

- **Keep recombination in each worker.** It is load-bearing: at long timelimits the
  search incumbent can drift into a poor basin (prob_20 @E=10000: 1.87M) and
  recombination is what rescues it (618k). A no-recombine worker would feed the
  master a poor candidate and lose to the current default on recombine-dependent
  instances (prob_6/12/20). This is now affordable because the pool cap
  (`SOLVER_POOL_PER_BAY`) bounds the MIP to ~8s and `SOLVER_CP_WORKERS=1` keeps each
  worker on one core (4 workers x 1 = 4 cores, no oversubscription).
- **No pool merging needed.** Each worker recombines on its OWN process-local `_POOL`
  built during its own search. This sidesteps the cross-process pool-merge problem
  that made the old "master-recombine-once" variant (V2) hard.
- Final scoring stays in the master only, so candidates are compared on one
  consistent materialization basis (avoids proxy incompatibility).

Initial environment inside each worker:

```text
SOLVER_CP_WORKERS=1      # one CP-SAT core per worker (4 workers -> 4 cores)
SOLVER_POOL_PER_BAY=1000 # keep the recombine MIP solvable within the 8s cap
# (do NOT set SOLVER_NORECOMB -- workers keep recombine on)
# thread-pool pinning (cheap insurance, set from the first run -- see section 10):
OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
NUMBA_NUM_THREADS=1
```

Keep these submission defaults unless testing a specific variant:

```text
SOLVER_MASK_SEARCH=1
SOLVER_MASK=1
SOLVER_NUMBA=1
SOLVER_MASK_PREPARE=1
SOLVER_UNIFIED_ILS=1
SOLVER_RECOMB_SOLVE_S=8
```

## 5. Portfolio Diversity

A portfolio only helps if the workers do meaningfully different work.

Initial 4 profiles:

| worker | seed | profile | knobs |
|---|---:|---|---|
| 0 | 12346 | random-balanced | default random perturb |
| 1 | 20265 | guided | `SOLVER_GUIDED=1` |
| 2 | 28184 | mixed-guided | `SOLVER_GUIDED=mix` |
| 3 | 36103 | late-perturb | lower init fraction, e.g. `SOLVER_UNIFIED_INIT_FRAC=0.4` |

The exact profile names can change, but the first experiment should keep the
portfolio simple and interpretable.

Candidate extra diversity knobs:

- different random seeds in `_perturb`
- `SOLVER_GUIDED` in `{unset, 1, mix}`
- `SOLVER_UNIFIED_INIT_FRAC` in `{0.4, 0.6, 0.75}`
- optional `SOLVER_MASK_SEARCH_R` in `{4, 8, 16}` for a later R-diverse portfolio

Do not mix too many dimensions in the first run. If a portfolio wins, run ablation
to identify which member contributed.

**Separate variance reduction from profile diversity.** In wall mode the same config
already varies 20-66% run-to-run (machine-speed-dependent stopping of a non-converged
ILS, not rng). So a portfolio of 4 *identical-config, different-seed* workers already
captures a good tail purely by best-of (variance reduction), before any knob diversity.
Run a **seed-only arm** (4x default config, seeds only) as a clean baseline alongside
the knob-diverse arm: if the knob-diverse portfolio does not beat seed-only, the gain is
just variance reduction and the extra profiles add nothing. See
`memory/eval-count-ab-protocol.md` and `memory/wall-clock-absolute-drift.md`.

## 6. Budgeting

The portfolio must respect the same external `timelimit`.

Suggested budget split for `timelimit=60`:

```text
safety reserve:       max(2.0s, 0.04 * timelimit)
master final scoring: adaptive, but reserve at least 6s
worker search window: remaining wall time
```

Example:

```text
timelimit = 60s
safety = 2.4s
master final reserve = 6.0s to 10.0s
workers run until about t0 + 50s
master gathers results and true-scores candidates before t0 + 57.6s
```

**Size the master reserve adaptively, not as a fixed fraction (updated 2026-06-17).**
The master true-scores up to `#candidates` assignments, each a full `_score_and_pack`
build. A fixed `final_guard` truncates those builds past `poly_deadline` and explodes
on big instances (this exact failure produced a prob_5 817k blow-up during the recombine
rework). Mirror the solver's `SOLVER_ADAPTIVE_RESERVE`: probe one build of the guaranteed
seed, then reserve for all candidates.

```text
build_cost   = time of one _score_and_pack(seed)           # measured up front
n_candidates = workers_completed + 1                        # + baseline
final_guard  = max(6.0, build_cost * n_candidates * margin) # margin ~1.5
worker_deadline = t0 + timelimit - safety - final_guard
```

If `final_guard` would eat too much of the budget on a slow-building instance, fall
back to scoring only `baseline + top-K workers by proxy` (see section 10), but treat
top-K as a separate variant because it can miss proxy-underranked winners.

If worker results arrive late:

- accept completed workers
- terminate or ignore unfinished workers after gather deadline
- always include the single-process seed/baseline candidate

The experiment should report:

- number of workers launched
- number of workers completed
- number of feasible/materialized candidates
- time spent in worker search
- time spent in master final scoring

## 7. Experimental Variants

### V0. Baseline

Current default single-process solver.

```text
algorithm(prob, timelimit)
```

### V1. Portfolio Search, Per-Worker Recombine (main experiment)

Four worker processes run the full diversified search + recombine pipeline; each
recombines on its own process-local `_POOL`.

```text
N=4
worker: SOLVER_CP_WORKERS=1, SOLVER_POOL_PER_BAY=1000  (recombine ON)
master: true-score returned (post-recombine) assignments + baseline, pick min
```

This is the main experiment. Updated from the original "no per-worker recombine"
plan: recombination is load-bearing (see section 4), and the pool cap makes it cheap
enough to run one-core-per-worker, so disabling it would handicap the portfolio
against the current default.

### V1b. No-Recombine Ablation (diagnostic only)

Same as V1 but workers run `SOLVER_NORECOMB=1`. Purpose: measure how much of the
portfolio's result is recombine vs search diversity. Expected to be worse than V1 on
recombine-dependent instances (prob_6/12/20). Not an adoption candidate.

### V2. Master Recombine Once (deprecated / low priority)

Originally: master runs `_recombine` once on the best worker assignment. With V1 doing
per-worker recombine on local pools, this is largely redundant and harder (the master's
`_POOL` does not contain worker-local pieces without explicit cross-process
reconstruction). Keep only if V1 shows that a pool merged across workers would help.

### V3. R-Diverse Portfolio

Workers differ by mask search resolution.

```text
worker 0: SOLVER_MASK_SEARCH_R=4
worker 1: SOLVER_MASK_SEARCH_R=8
worker 2: SOLVER_MASK_SEARCH_R=16
worker 3: default R=8 with guided/mix
```

This tests whether the known instance-dependent R behavior can be captured by a
portfolio rather than an adaptive classifier.

Run only after V1 establishes the portfolio harness.

### V4. Fewer Workers

Compare:

```text
N=2
N=3
N=4
```

Purpose:

- detect overhead
- determine whether 4 workers actually saturate the environment
- avoid making the default worse on systems where only 2 cores are available

## 8. Measurement Protocol

Use two complementary protocols.

### 8.1 Wall-Clock Protocol

Primary protocol because the portfolio is a wall-time strategy.

Run full train set:

```text
20 instances
timelimit = 60s
same machine
same environment
one instance per process group
```

Repeat at least 3 times if wall variance is high.

Report:

- total normalized objective
- per-instance objective
- feasibility/stage
- wall time
- worker completion count
- selected worker id/profile

### 8.2 Fixed-Eval Diagnostic

Fixed-eval mode is useful for algorithmic changes inside a single process, but it
is less natural for a multiprocessing portfolio because total evaluations scale
with worker count.

Use it only for diagnostics:

- compare worker profiles at equal per-worker eval count
- check whether a profile is inherently better or only benefits from more wall
  time

Do not use fixed-eval mode as the final portfolio verdict unless total eval budget
is normalized across workers.

## 9. Success Criteria

Adopt as default only if V1 or a later variant satisfies all conditions:

- full-20 feasible on all instances
- aggregate objective improves at BOTH timelimits tested: at least 3% at `T=60` AND
  at least 3% at `T=300` (the portfolio must not help at one limit while hurting the
  other, since the contest timelimit is unknown). A larger gain is expected at `T=300`
  where single-trajectory drift is worse and spawn/JIT overhead amortizes.
- no severe per-instance regression above 10% unless offset by a large and stable
  aggregate gain
- median wall time stays within the evaluator limit with safety margin at both limits
- repeated wall-clock runs show the gain is not a one-run variance artifact (and note
  the portfolio itself should REDUCE run-to-run variance vs single-process)

Stronger adoption case:

- aggregate gain >= 5%
- selected workers are diverse across instances
- no instance repeatedly fails due to timeout or missing worker results

Reject or keep experiment-only if:

- aggregate gain < 2%
- wins are explained mostly by using more than the allowed CPU budget
- master final scoring frequently times out
- worker spawn/JIT overhead consumes too much of `T=60`
- results depend on unstable OS scheduling behavior

## 10. Risks and Controls

### CPU Oversubscription

Risk:

```text
4 Python workers * 4 CP-SAT workers = 16 native workers
```

Control:

```text
worker env: SOLVER_CP_WORKERS=1   # 1 CP-SAT core/worker -> 4 workers = 4 cores
# V1 keeps recombine ON (pool cap bounds it); only V1b sets SOLVER_NORECOMB=1
```

Also pin native threadpools in the worker env from the first run (cheap insurance,
also listed in section 4):

```text
OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
NUMBA_NUM_THREADS=1
```

### Windows Multiprocessing Spawn Cost

Risk:

- each child imports solver
- numba cached functions may still pay warm-up overhead
- large `prob` dict is pickled to every worker

Control:

- The solver's numba functions are `@njit(cache=True)`, so the compiled code is
  written to an on-disk cache. **The master should warm the cache before spawning
  workers** — call the jitted functions once on a tiny input in the master — so the 4
  workers LOAD from the disk cache instead of a thundering-herd simultaneous compile.
  Caveat: on the contest's first instance the cache is cold, so the master pays one
  compile up front; subsequent instances reuse the warm cache.
- thread-pool env vars (`OMP/MKL/OPENBLAS/NUMBA_NUM_THREADS=1`) are set in the default
  worker env from the first run (moved out of "later" — cheap insurance, see section 4).
- first test at `T=60`, not tiny smoke budgets
- record worker startup time (gate the experiment on it: if spawn+load eats a large
  fraction of `T=60`, the portfolio is only viable at longer limits)
- keep worker payload simple (pickling `prob` is cheap: 100-300 blocks)

### Incompatible Candidate Scoring

Risk:

- worker proxy scores are not comparable to final materialized scores

Control:

- master always selects by `_score_and_pack`
- include default seed/base candidate in the master candidate list

### Pool Loss

Risk:

- recombination's value depends on a rich `_POOL`; a portfolio splits the search across
  processes, so each worker's pool is smaller than a single-process run's pool

Control:

- V1 keeps per-worker recombine ON, each on its own process-local pool (no merging) —
  the pool cap means a per-worker pool is already enough (recombine plateaus within a
  few thousand columns), so the per-worker pool being smaller is not expected to hurt
- V1b (no-recombine ablation) isolates how much recombine contributes
- cross-process pool merging is only worth exploring (old V2) if V1b shows the smaller
  per-worker pools are leaving recombine gains on the table

### Timeout During Final Guard

Risk:

- true-scoring 4 worker assignments plus baseline can be too slow on hard cases

Control:

- cap number of master-scored candidates
- score candidates in proxy rank order, but never rely solely on proxy rank
- always keep an already-materialized fallback if available

Initial cap:

```text
score at most 5 candidates: baseline + 4 workers
```

If final scoring is too slow:

```text
score baseline + top 2 worker candidates by proxy
```

but mark this as a separate variant because it can miss proxy-underranked winners.

## 11. Implementation Plan

### Step A. Refactor Search Return Path

Add an internal helper that returns an assignment instead of a solution.

Target shape:

```python
def search_assignment(prob, timelimit, profile=None):
    ...
    return best_assign, metadata
```

It should reuse the current `framework_solve` search logic as much as possible.
Avoid maintaining two divergent algorithms. The refactor is small: `framework_solve`
already computes `best` (the post-recombine assignment) and `base_incumbent` just
before its final true-objective guard (the `_score_and_pack(best) vs
_score_and_pack(base_incumbent)` block). Expose a path that returns
`(best, base_incumbent, metadata)` instead of running that guard + emission, and let
the **master** run the final guard across ALL workers' `best` and `base_incumbent`
candidates plus the guaranteed seed (one consistent materialization). This keeps the
single-process default path byte-for-byte unchanged when the portfolio gate is off.

### Step B. Add Worker Entry

Use `multiprocessing` with spawn-safe top-level functions.

Worker function:

```python
def _portfolio_worker(payload):
    prob, timelimit, profile = payload
    set profile env/knobs
    run search_assignment()
    return result
```

No nested functions as process targets on Windows.

### Step C. Add Master Portfolio Wrapper

Experiment gate:

```text
SOLVER_PORTFOLIO=1
SOLVER_PORTFOLIO_WORKERS=4
```

`myalgorithm.algorithm()` can keep default behavior unless the gate is set.

### Step D. Logging

Write compact JSONL event logs when `SOLVER_TRACE` or a new portfolio trace flag
is enabled.

Useful events:

```text
portfolio.start
portfolio.worker.start
portfolio.worker.done
portfolio.gather.done
portfolio.score.candidate
portfolio.selected
```

### Step E. Evaluation Harness

Add scripts or commands that run:

```text
baseline default
portfolio V1
portfolio V1 repeated
```

Keep output files separate and include env settings in the result metadata.

## 12. Initial Run Matrix

Smoke:

| run | instances | T | purpose |
|---|---:|---:|---|
| smoke-startup | 1 hard | 60 | measure spawn + numba-cache-load cost (gate) |
| smoke-v1 | 3 hard + 2 easy | 60 | correctness, no timeout, master reserve OK |
| smoke-n2 | same | 60 | overhead check |
| smoke-n4 | same | 60 | CPU utilization and quality |

Full (run at BOTH timelimits — the contest limit is unknown):

| run | instances | T | repeats | purpose |
|---|---:|---:|---:|---|
| baseline-60 | 20 | 60 | 3 | current variance at 60s |
| baseline-300 | 20 | 300 | 3 | current variance + high-budget drift at 300s |
| v1-n4-60 | 20 | 60 | 3 | main verdict at 60s (spawn overhead matters here) |
| v1-n4-300 | 20 | 300 | 3 | main verdict at 300s (biggest expected win) |
| v1b-norecomb | 20 | 300 | 1 | ablation: recombine vs search-diversity contribution |
| seed-only | 20 | 300 | 1 | isolate variance reduction from knob diversity |
| v1-n2 | 20 | 60 | 1 | worker-count sensitivity |
| v3-rdiverse | 20 | 60 | 1 | optional follow-up |

Recommended hard smoke instances:

```text
prob_6, prob_7, prob_12, prob_18, prob_20
```

These have historically exposed drift, variance, or geometry-sensitive behavior.

## 13. Result Table Template

Per-instance:

| instance | baseline obj | portfolio obj | delta | selected profile | completed workers | wall |
|---|---:|---:|---:|---|---:|---:|
| prob_1 | | | | | | |

Aggregate:

| run | feasible | total obj | delta vs baseline | median wall | p95 wall |
|---|---:|---:|---:|---:|---:|
| baseline | | | | | |
| v1-n4 | | | | | |

Worker contribution:

| profile | selected count | best count before true-score | avg final delta when selected |
|---|---:|---:|---:|
| random-balanced | | | |
| guided | | | |
| mixed-guided | | | |
| late-perturb | | | |

## 14. Decision Rules

After the first full run:

1. If V1 improves >= 5% and no repeated severe regression appears, proceed to V2
   and V3.
2. If V1 improves 2-5%, inspect worker selection and timeout logs before changing
   code defaults.
3. If V1 is flat but some profiles win specific instances, keep the harness for
   R-diverse or guided-profile follow-up.
4. If V1 regresses or times out, reject 4-process portfolio and test N=2 only if
   logs show spawn/final-score overhead was the cause.

Default adoption requires a second full-20 confirmation run.

## 15. Notes

This experiment is most likely to help when:

- the single search trajectory falls into a poor basin
- guided or R-diverse behavior has complementary wins
- the final materializer can score all worker candidates within the reserved time

It is least likely to help when:

- the instance is already solved well by seed/local search
- all workers converge to the same assignment
- final scoring consumes the saved search time
- multiprocessing startup dominates the budget

The safest first target is not a full default replacement. It is an env-gated
portfolio harness that can produce evidence without destabilizing the current
submission path.
