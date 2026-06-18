# Incremental (Delta) Search-Evaluation Experiment Design

> Created 2026-06-17. Goal: make the per-candidate objective evaluation in the search
> loop **incremental** (touch only the bays a move changes) instead of recomputing the
> whole objective from the full assignment every candidate. This is a **pure-speed**
> lever: at a fixed eval count the objective must be **bit-identical**; the payoff is
> lower wall -> more candidate evaluations at a fixed timelimit -> better objective.

## 0. Purpose

The search loop (`local_search`, `improved_search`, `_climb`) evaluates a candidate
move with:

```python
trial = dict(cur)          # O(n) copy of the whole assignment, every candidate
trial[i] = j
tot, _ = total_obj(prob, trial, cache)   # full objective recompute
```

A single-block move (`i: a -> j`) changes only **two** bays (source `a`, destination
`j`). Yet `total_obj` recomputes the objective from the entire assignment each time.
This experiment replaces the full recompute with an incremental delta.

## 1. Current Behavior (precise)

`total_obj -> eval_obj1 (Z1) + obj23 (Z2,Z3)`:

- **`eval_obj1` (Z1, tardiness, lines ~655-673):**
  ```python
  for j in range(m):                                  # ALL bays
      ids = tuple(sorted(i for i,a in assign.items() if a==j))  # full assign scan, O(n)
      perbay[j] = cache[ids] if ids in cache else solve_bay(...)   # packing memoized by set
      _POOL[(j, ids)] = perbay[j]                     # SIDE EFFECT: feeds recombination
      obj1 += perbay[j]
  ```
  Packing is already incremental (cache keyed by a bay's block-set: only the 2 changed
  bays miss and re-pack). But the **bookkeeping is full every call**: rebuild all `m`
  id-sets (each a full O(n) scan -> O(m*n)), `m` cache lookups, full sum.
- **`obj23` (Z2,Z3, lines ~638-652):** full O(n) scan of all blocks to rebuild `loads`
  and the preference sum; Z2 = `floor(max over pairs |u[a]*load[a]-u[b]*load[b]|)`.
- **Per candidate, removable overhead** = `dict(cur)` copy (O(n)) + id-set rebuild
  (O(m*n)) + Z2/Z3 scan (O(n)). For n=300, m=5 that is ~2k ops + a 300-entry dict
  allocation, **per candidate**, on top of the (unavoidable) packing of the 2 changed
  bays.
- Extra: on every accepted move the loops call `total_obj` a **second time** just to
  refresh `perbay` (lines ~766, ~869) -- redundant given the trial's perbay.

`_EVALS` is incremented once per `total_obj` call (line ~678); `SOLVER_MAX_EVALS`
stops the search at a fixed count. `_TRACE` records improving totals (line ~683).

## 2. Cheap Falsification First (Step 0 -- do this before refactoring)

The unavoidable cost per candidate is packing the 2 changed bays (`solve_bay`). The
**ceiling** on this lever is the fraction of per-candidate time spent in the removable
bookkeeping (dict copy + id rebuild + Z2/Z3), NOT in packing. If packing dominates
(e.g. bookkeeping < 10% of per-candidate time), the refactor is not worth its risk.

Probe (cheap): instrument one `total_obj` call to time (a) the `dict(cur)` copy,
(b) `eval_obj1` minus the `solve_bay` calls (pure bookkeeping), (c) the `solve_bay`
calls, (d) `obj23`. Run on a few instances (small/large) mid-search. Decision:

- bookkeeping (a+b-without-pack+d) >= ~25% of per-candidate time -> proceed.
- < ~10% -> shelve (packing-bound; spend effort on faster packing instead).

Note the cache-hit structure helps the bookkeeping fraction: when trying many targets
`j` for a fixed mover `i`, the source bay `a`'s new set (a minus i) is identical across
targets -> cached after the first -> later candidates pay ~1 `solve_bay` + full
bookkeeping, so bookkeeping is a larger share there.

## 3. Hypothesis

Maintaining the objective incrementally produces the **same** objective at a fixed eval
count and lowers wall time, so at a fixed timelimit the search runs more candidates and
reaches a better objective. Pure-speed lever, same class as numba / `shapely.prepare`.

## 4. What Is Incremental-able (and exactly bit-identical)

A move `i: a -> b` (a = old bay, b = new bay):

- **Z1 (tardiness):** `obj1_new = obj1 - perbay[a] - perbay[b] + T(a\{i}) + T(b∪{i})`.
  The two new per-bay tardiness values come from `solve_bay` (already cached by set).
  `tard` is `sum(max(0, exited[i]-due[i]))` over integer time steps and integer due
  dates -> **integer-valued**, exactly representable in float64, so the add/subtract
  delta is **exact** (no FP drift) as long as totals stay < 2^53 (they do).
- **Z2 (load imbalance):** maintain `loads[j]` incrementally
  (`loads[a]-=w_i; loads[b]+=w_i`), then **recompute Z2 fully from `loads`** each move
  (O(m^2), m<=5 -> trivial, and exact since it is the same float expression as the full
  path). Z2 must NOT be delta-accumulated (it is a global min-max, not a sum).
- **Z3 (preference):** `z3_new = z3 + pref_i[a] - pref_i[b]` (integer delta, exact).

So with `loads`, `z3`, `perbay[*]`, `obj1` carried as running state, a candidate's total
is computed in **O(pack 2 bays + m^2 + O(1))** with **no full-assignment scan and no
dict copy**, and is **bit-identical** to the current full recompute.

Caveat: `workload` must be integer for `loads` deltas to stay exact across many moves;
verify in Step 0 (if float, recompute `loads` from scratch every K moves, or accept and
prove bit-identity empirically).

## 5. Architecture

An incremental evaluator that the search loops drive with apply/try, not dict rebuilds:

```python
class EvalState:
    # holds: assign, loads[m], z3, perbay[m], obj1, cache, prob, weights, u[m]
    def total(self): ...                  # w1*obj1 + w2*Z2(loads) + w3*z3
    def delta_total(self, i, j):          # total IF i moved to j, WITHOUT mutating
        # recompute only perbay for (cur[i]\{i}) and (j∪{i}) via solve_bay(cache),
        # loads/z3 deltas, Z2 from trial loads; increments _EVALS; records _POOL pieces
    def apply(self, i, j): ...            # commit the move, update running state
```

The loops change from "build trial dict + total_obj" to "evaluate delta_total(i, j),
and apply(i, j) on improvement". `delta_total` must preserve the existing side effects:

- increment `_EVALS` exactly once per candidate (so `SOLVER_MAX_EVALS` stops at the same
  logical point);
- record the changed bays' `(j, ids) -> T` into `_POOL` (recombination pool) the same way
  `eval_obj1` does -- otherwise the recombine pool differs and the run is NOT
  bit-identical;
- feed `_TRACE` on improvement the same way `total_obj` does.

Gate: `SOLVER_INCR_EVAL` (default off -> the current full-recompute path is byte-for-byte
unchanged). On only inside the search loops.

## 6. Correctness and Bit-Identity (the hard gate)

This is a behavior-invariant speedup, so the acceptance gate is **bit-identical objective
at a fixed `SOLVER_MAX_EVALS`** across all 20 instances, on vs off. Any single mismatch is
a bug, not a tradeoff. Specific things that break bit-identity if mishandled:

1. **`_POOL` side effect** -- the incremental path must populate `_POOL` with the same
   `(bay, set)` pieces eval_obj1 would, or recombination sees a different pool -> different
   adopted assignment -> mismatch. (This is the easiest thing to forget.)
2. **`_EVALS` counting** -- must increment once per candidate, identically, or the eval-
   count stop fires at a different point.
3. **FP drift** -- avoided by the integer-exactness argument (section 4) + recomputing Z2
   from loads rather than accumulating it. Validate empirically.
4. **Tie-breaking / move order** -- the loops must visit movers/targets in the SAME order
   and apply the SAME `< best - 1e-9` acceptance, so the trajectory is identical.

Debug assertion mode (`SOLVER_INCR_EVAL_CHECK`): every K candidates, recompute the full
`total_obj` and assert it equals the incremental total (and `perbay`, `loads`, `z3`
match). Run this during development on all 20, then drop for the timed runs.

## 7. Scope

Refactor the three hot loops, gated, smallest first:

1. `local_search` (simplest: single mover-set, single target loop).
2. `_climb` (adds maxload / off-pref movers, preference-ordered targets).
3. `improved_search` (two-phase; reuses the same EvalState).

Keep one evaluator implementation so the full and incremental paths cannot diverge in
the objective definition (the incremental path IS the full objective, computed by delta).

## 8. Risks and Controls

- **Hidden side effect drift** (`_POOL`/`_EVALS`/`_TRACE`) -> covered by section 6 +
  bit-identity gate. Highest-risk item.
- **State desync** (loads/perbay/z3 diverge from `assign` after many apply/revert) ->
  `SOLVER_INCR_EVAL_CHECK` assertion mode catches it.
- **Modest payoff** -> Step 0 gates this; do not refactor if packing-bound.
- **Refactor regression on the default path** -> env-gated, default off, full-20 bit-
  identity proves the off-path is unchanged and the on-path matches.
- **`solve_bay` cache semantics** unchanged (same set-keyed cache); the incremental path
  must use the SAME cache so packing results are shared with the full path.

## 9. Implementation Plan

- **Step 0.** Profile per-candidate time split (section 2). Gate the whole experiment.
- **Step A.** Add `EvalState` with `total`, `delta_total(i, j)`, `apply(i, j)`,
  preserving `_EVALS`/`_POOL`/`_TRACE`. Add `SOLVER_INCR_EVAL` + `..._CHECK` gates.
- **Step B.** Rewrite `local_search` to use `EvalState` when gated; keep the legacy body
  for the off path (or route both through EvalState but verify off-path bit-identity).
- **Step C.** Validate: full-20 `SOLVER_MAX_EVALS` fixed, obj on==off bit-identical;
  `..._CHECK` clean. Then measure wall delta at fixed eval count.
- **Step D.** Extend to `_climb` and `improved_search`; re-validate bit-identity.
- **Step E.** Remove the redundant second `total_obj` on accept (use the accepted trial's
  perbay) -- only after bit-identity holds, and re-validate.

## 10. Measurement Protocol

Per `docs/methodology.md` 1b and `memory/eval-count-ab-protocol.md`:

- **Bit-identity gate (quality):** fix `SOLVER_MAX_EVALS=N`, run full-20 on and off.
  Objective MUST be identical on every instance. This is the correctness gate.
- **Speed (the payoff):** at that fixed N, report wall on vs off -> the per-eval speedup.
- **Quality conversion:** at a fixed wall `timelimit` (60s and 300s), the freed time
  becomes more evaluations -> lower objective. Report obj + eval-count reached, on vs off.
  This is the actual benefit (same logic as the numba / prepare adoptions).

## 11. Run Matrix

| run | instances | mode | purpose |
|---|---|---|---|
| profile | 3 (small/med/large) | mid-search timing | Step 0 gate |
| bitident | 20 | `SOLVER_MAX_EVALS=2500`, on vs off | correctness gate (must match) |
| check | 5 | `..._CHECK=1` | state-desync assertions clean |
| speed | 20 | `SOLVER_MAX_EVALS=2500`, wall on vs off | per-eval speedup |
| quality-60 | 20 | wall T=60, on vs off | objective at fixed wall |
| quality-300 | 20 | wall T=300, on vs off | objective at fixed wall |

## 12. Success Criteria

- **Hard gate:** bit-identical objective at fixed eval count on all 20 (else: bug, fix or
  reject).
- **Adopt** if bit-identical AND wall at fixed eval count drops materially (e.g. >= 10%)
  AND fixed-wall objective improves or is neutral. As a pure-speed lever it can only help
  (more evals) or be neutral; a fixed-wall regression would indicate a hidden behavior
  change (investigate).
- **Shelve** if Step 0 shows packing-bound (< ~10% bookkeeping) or the wall gain is within
  noise.

## 13. Decision Rules

1. Step 0 says packing-bound -> stop, document, redirect to faster packing.
2. Bit-identity fails -> it is a correctness bug (likely `_POOL`/`_EVALS`/FP); fix before
   any timing claim.
3. Bit-identical + >=10% per-eval wall cut -> extend to all three loops, run fixed-wall
   quality at 60s and 300s, adopt as default (gate on in `submission/myalgorithm.py`).
4. Composes with the parallel portfolio (faster per-worker search) and is orthogonal to
   the recombine/guard work.

## 14. Notes

- This is a speed lever, not an algorithm change: the search trajectory is identical, only
  cheaper to compute. That is exactly why bit-identity is both required and sufficient.
- Biggest single removable cost may be the `dict(cur)` copy per candidate (a full-size
  dict allocation), not just the scans -- the delta API mutates running state instead.
- Pairs naturally with removing the redundant post-accept `total_obj` (Step E).

## 15. Step 0 Result (2026-06-17)

Ran the cheap-falsification profile before refactoring, on the local Windows runtime
created for this experiment:

- Python 3.9 via `conda run -n base`, with workspace-local target packages in
  `.codex_runtime39_lib`.
- `SOLVER_MASK_SEARCH=1`, `SOLVER_MASK=1`, `SOLVER_MASK_PREPARE=1`,
  `SOLVER_NUMBA=1`.
- Instances: `prob_1`, `prob_13`, `prob_20`; 80 candidate move evaluations each.
- Output: `.claude/scratch/incr_eval/profile_numba.jsonl`.

| instance | samples | accepted | removable % (`copy + eval_obj1 bookkeeping + obj23`) | solve_bay % |
|---|---:|---:|---:|---:|
| `prob_1` | 80 | 8 | 8.55% | 91.45% |
| `prob_13` | 80 | 17 | 7.59% | 92.41% |
| `prob_20` | 80 | 21 | 4.83% | 95.17% |
| **ALL** | 240 | 46 | **6.39%** | **93.61%** |

Decision per section 2 / section 13: **shelve incremental-eval for now**. The candidate
evaluation is packing-bound under the current mask-search stack; the removable bookkeeping
is below the 10% stop threshold overall and especially on the large hard instance. The
next speed work should target `solve_bay` / packing cost, not the full-objective
bookkeeping refactor.

## 16. Fixed-Eval Bit-Identity Probe (2026-06-17)

After the Step 0 result, temporarily added a gated prototype (`SOLVER_INCR_EVAL=1`) to
answer whether the incremental evaluator can at least preserve behavior at fixed eval
count. The prototype was removed after the negative speed result; only the experiment
record remains.

Configuration:

- Same local runtime and stack as section 15.
- The temporary `EvalState` prototype was wired into `local_search`, `improved_search`,
  and `_climb` only when `SOLVER_INCR_EVAL=1`.
- The legacy accepted-move refresh eval is still counted, so `SOLVER_MAX_EVALS` stops at
  the same logical point.
- Outputs:
  - `.claude/scratch/incr_eval/full20_E500_{off,on}.jsonl`
  - `.claude/scratch/incr_eval/hard_E2500_{off,on}.jsonl`

Results:

| run | instances | eval budget | mismatches | wall off | wall on | wall delta |
|---|---:|---:|---:|---:|---:|---:|
| full20 | 20 | 500 | 0 | 335.16s | 334.64s | -0.16% |
| hard subset (`prob_13/14/20`) | 3 | 2500 | 0 | 306.42s | 314.89s | +2.76% |

Conclusion: fixed-eval behavior is preserved on the tested matrix (objective,
feasibility, and eval count match exactly), so the idea does **not** make quality worse
under a fixed evaluation count. It still should **not** be adopted as a speed lever: the
full20 E=500 speed is noise-level, and the hard E=2500 subset is slower. The prototype was
discarded from `submission/solver.py`. This confirms the Step 0 diagnosis that the current
bottleneck is `solve_bay`, not full-objective bookkeeping.
