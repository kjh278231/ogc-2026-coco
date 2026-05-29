# OGC 2026 — Improvement Log

Approved improvements to the Hermes solver (`baseline/myalgorithm.py`).
Latest entry first. An entry is added only after the eval → analyst →
strategist → developer → re-eval → approval-gate pipeline reaches an
APPROVE or REVIEW-lean-approve verdict and the change is merged.

Schema per entry:
- **Verdict** — APPROVE / REVIEW-lean-approve (with caveats)
- **Hypothesis** — one-line thesis
- **Change locus** — file + function the patch targets
- **Baseline vs target** — run_id comparison, key metrics
- **Commit** — short SHA on `feature/my-algorithm`
- **Caveats / follow-ups** — open risks, deferred work

---

## H-001 — Skip edd_retry fallback, route directly to forced placement

- **Date merged**: 2026-05-30
- **Verdict**: REVIEW (lean-approve) — bench mean ratio 0.857, zero regression, two soft fails procedural only
- **Hypothesis**: When all four init heuristics (EDD, SlackRatio, MST, LargestArea) fail at stage 2 on hard bench_B5 instances, the `edd_retry` fallback re-runs the same EDD heuristic with extended budget — burning ~20s without progress before the inevitable `forced` fallback. Skipping `edd_retry` and jumping straight to forced placement frees ~18s of wall-clock for simulated annealing.
- **Change locus**: `baseline/myalgorithm.py` — the `if best_perm is None:` block inside `algorithm()` (formerly lines 608–625). Bundled with prior uncommitted scaffolding (JSONL event-log `_emit`, OBB local-poly cache, `_ACTIVE_DEADLINE` propagation in `baseline_greedy.py`, SA loop refactor `time_budget`→`search_deadline`, hoisted `tight_blocks` precompute).
- **Baseline (run_2) vs target (run_3)** — pattern=`bench_B5_*.json`, timelimit=30s:

  | Instance | obj before | obj after | Δ% | sa_iters before | sa_iters after | sa_improvements |
  |---|---:|---:|---:|---:|---:|---:|
  | bench_B5_b120_preference_skew | 14,325,851 | 10,216,451 | **−28.7%** | 0 | 9 | 3 |
  | bench_B5_b150_mixed_hard | 32,619,533 | 32,619,533 | 0.0% | 0 | 9 | 0 |

  Bench mean obj ratio: **0.857**. Feasibility preserved (both stage 5). Fallback still triggered (now `forced_direct` instead of `edd_retry`→`forced`).

- **Commit**: `4972d5f` on `feature/my-algorithm`
- **Caveats / follow-ups**:
  - Smoke instances were **not** re-run under H-001 (R2 procedural soft fail). Architectural argument: `edd_retry` path only fires when all seeds fail stage 2, which does not occur on smoke. Data-backed confirmation deferred.
  - R4 SA-throughput rule tripped because baseline `sa_iterations=0` was the pathology the patch addressed; the rule cannot distinguish "SA eliminated" from "SA was never reachable". Direction is strictly positive (0 → 9).
  - **b150 unchanged**: SA's swap/insert/invert neighborhood cannot escape the forced placement's local optimum on this instance. Next candidate hypothesis: coarser neighborhood (block re-assignment to a different bay) **or** H-002 (insert a `repair_mode='simple'` seed before the SA loop to provide a non-degenerate starting point).
  - The solver-developer agent made off-locus edits beyond the strict `best_perm is None` block (SA loop refactor, dead-code removal). Behavior verified by eval; flagged here for future hygiene.

---
