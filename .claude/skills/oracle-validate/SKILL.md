---
name: oracle-validate
description: Use whenever you have a candidate solution or an "improved" algorithm and want to claim it is feasible / better. Never trust a self-computed objective; validate with utils.check_feasibility across instances, and guard every improvement against regression. Use before reporting any result as a win.
---

# Oracle-validate (+ non-regression guard)

Self-computed metrics can be wrong (a real assembly bug here produced a great-looking
tardiness that the oracle rejected as INFEASIBLE). Only `utils.check_feasibility` is ground
truth. And because local search is path-dependent, an "improvement" can silently regress on
some instances — guard against it.

## Procedure

1. **Assemble the full solution dict** and call the real oracle:
   `utils.check_feasibility(prob, solution)` → must report `feasible: True`; use its
   `objective / obj1 / obj2 / obj3`, not your own numbers.
   - Gotcha: the checker reconstructs records in *insertion order*. Build `operations` with
     time keys in **sorted** order and EXIT-before-ENTRY within a key, or you get spurious
     "no EXIT" Stage-1 failures.
   - Gotcha: integer placement bounds must be `lower=ceil(max(0,−min_vert))`,
     `upper=floor(W−max_vert)`; `round`/`int`-truncation violates the bay boundary (Stage 2).

2. **Validate across ALL instances, not a lucky subset.** Run the full batch
   (`framework.py 50 "train/*.json"`). Require **feasible on every instance** and runtime
   within the competition limit (per-problem, minutes–30 min; we target well under).

3. **Non-regression guard.** Compare the new result to the previous validated result
   *per instance, at the same time budget* (different budgets are not comparable). If the
   change can regress (any search/heuristic change), keep the **best-of** the new and the
   previously-validated approach (shared cache makes this cheap) so you never fall below the
   established floor.

4. **Report honestly:** state feasibility count (e.g. 20/20), per-instance objective deltas
   (including any regressions), and wall-clock vs budget.

## Reference
`.venv/Scripts/python.exe`; `docs/methodology.md` §1G, §3.
