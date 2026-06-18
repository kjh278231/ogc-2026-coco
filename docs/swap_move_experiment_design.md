# Guided Swap-Move Experiment Design

> Created 2026-06-17. Add a **swap** (exchange two blocks between bays) move to the
> search neighborhood, with **signal-guided candidate selection** (not random pairs).
> The search today only relocates a single block (a -> b); a swap reaches improving
> configurations a single relocation cannot. This is a **behavior-changing** lever:
> judge it by the objective at a fixed eval count (does the richer neighborhood find a
> better solution within the same budget?), with wall as the cost.

## 0. Current Behavior

The search mutates the assignment in exactly one way -- a single-block relocation:

```text
local_search   L762:  trial[i] = j
improved_search L817: trial[i] = j
_climb         L865:  trial[i] = j
_perturb       L912:  cand[i] = rng.choice(opts)   # k independent relocations, not swaps
```

There is **no swap operator** anywhere. A relocation moves block i's whole workload from
its bay to another; the assignment is always feasible (bays pack independently), so swaps
are not needed for feasibility -- they are needed to reach optima relocation misses.

## 1. Why a Swap (what relocation cannot do)

This is a bay-assignment problem; every assignment is feasible and the objective is
`w1*Z1(tardiness) + w2*Z2(load imbalance) + w3*Z3(preference)`.

- **Z2 granularity:** a relocation shifts load by +-w_i (coarse); when the imbalance is
  small, every single move over-corrects and none improves. A swap of i and k shifts load
  by +-(w_i - w_k) -- fine control -> crosses Z2 local optima relocation is stuck at.
- **Saturated bays:** if bay a is tardy and bay b is near-tardy, a -> b relocation makes b
  worse (no net gain), but exchanging a's worst block for one of b's tolerable blocks can
  cut a without breaking b.
- **Deeper local search:** a relocation-local-optimum need not be a swap-local-optimum, so
  swaps deepen `_climb` / `local_search` and diversify Destroy.

## 2. Why Guided, Not Random

The swap neighborhood is O(n^2) pairs vs O(n*m) relocations. Random pairs are mostly
non-improving -> wasted evaluations. Guide with cheap, informative signals so only
promising pairs are evaluated.

**The informative signal is the post-packing exit time, not input slack.** Exp0 found the
input temporal slack `due - (release + processing)` is essentially 0 for almost every
block (everything is tight) -- vacuous as a discriminator. Tardiness here is positional /
crane-driven, so the block that actually exits late is the congested/trapped one, and that
only shows up *after* packing. We already compute it: `extract_tardiness` returns
`(tard, exited)` and `eval_obj1` currently discards `exited` (`T, _ = ...`). Caching
`exited` gives, per block:

```text
tard_i  = max(0, exited_i - due_i)   # how late this block is
slack_i = due_i - exited_i           # how early (room to absorb congestion)
```

## 3. Guided Candidate Families

One family per objective term; generate from the family matching the dominant current
term (largest `w*Z` component, cheap to know) -- or take top-K from each. A swap pairs a
"swap-out" candidate with a "swap-in" candidate from different bays.

**(A) Z1 -- tardiness <-> slack** (the primary idea)
- swap-out: highest `tard_i` block in a tardy bay (the congestion driver)
- swap-in: highest `slack_i` block in a non-tardy bay (can absorb congestion)
- intent: the tardy bay sheds its worst block for a tolerant one; the donor bay loses a
  block (less congestion).

**(B) Z2 -- heavy <-> light for load balance**
- swap-out: heaviest `workload` block in the over-loaded bay (max `u*load`)
- swap-in: lightest block in the under-loaded bay
- the `w_i - w_k` shift fine-tunes the imbalance.

**(C) Z3 -- mutually mis-preferring pair**
- i in bay a but prefers b, and k in bay b but prefers a -> swapping improves both
  preferences. Rank by per-block preference loss `max(pref) - pref[current]`, pair across
  bays where each prefers the other's bay.

**Cross-term ranking:** the strongest swaps improve several terms (e.g. a tardy bay that is
also over-loaded: heavy+late out, light+slack in helps Z1 and Z2). Rank candidates by a
combined score (e.g. `tard_i + alpha * u*workload contribution + beta * pref loss`) so
multi-term wins surface first.

**Keep it small:** take **top-K** swap-out and **top-K** swap-in (e.g. K=5..10), evaluate
the K*K pairs (or top-K aligned pairs) -> O(K^2), not O(n^2).

## 4. Signal Is a Filter, Acceptance Is the Truth

Tardiness is positional and recomputed by `solve_bay` for both changed bays, so a
"slack block absorbs congestion" guess is not guaranteed -- the swap is only adopted if
the real delta passes `< best - 1e-9`. The signal exists solely to narrow which pairs we
evaluate; a wrong guess is just a rejected candidate, never a regression.

## 5. Architecture

A swap of i (bay a) and k (bay b) changes exactly two bays:

```text
a' = (a \ {i}) ∪ {k}
b' = (b \ {k}) ∪ {i}
```

So its evaluation cost equals a relocation's (re-pack 2 bays; both via the existing
set-keyed cache). Per-candidate delta:

```text
Z1: obj1 - perbay[a] - perbay[b] + T(a') + T(b')
Z2: loads[a] += w_k - w_i ; loads[b] += w_i - w_k ; recompute Z2 (O(m^2))
Z3: + (pref_i[a] - pref_i[b]) + (pref_k[b] - pref_k[a])
```

API: `try_swap(i, k)` returns the resulting total without mutating; `apply_swap(i, k)`
commits. This is the same shape as the relocation delta -- so it slots into the
**incremental-eval evaluator** (`docs/incremental_eval_experiment_design.md`) directly.

## 6. Where It Plugs In

- **Hill-climb neighborhood:** in `local_search` / `_climb`, after the relocation sweep
  finds no improving relocation, run a guided-swap sweep (top-K pairs); accept first
  improvement; continue until neither relocation nor swap improves. (Swaps as a
  second-tier neighborhood -> deeper local optima.)
- **Destroy variant:** a guided swap as an ILS kick (exchange a tardy-bay driver with a
  slack block) -- a more targeted perturbation than random re-homing. Compare against the
  current random Destroy as a portfolio profile.

Gate: `SOLVER_SWAP` (default off; bit-identical when off). Sub-knobs: `SOLVER_SWAP_K`
(top-K), `SOLVER_SWAP_FAMILIES` (subset of {z1,z2,z3}).

## 7. Sequencing (important)

The **quality question** -- "does a guided swap neighborhood find a better solution at the
same eval budget?" -- is answerable **now, with the current full-recompute eval**, because
the eval-count protocol is deterministic regardless of per-eval speed. Swap does NOT block
on incremental-eval for its verdict.

Incremental-eval is the **deployment speed multiplier**: it makes each swap as cheap as a
relocation so the wall cost is acceptable. So: prove swap quality first (eval-count), then
let incremental-eval pay for it at fixed wall.

## 8. Correctness / Feasibility

- Feasibility is never at risk: any bay assignment is feasible; a swap is two relocations.
- Behavior-changing: the trajectory changes, so the objective changes -- this is the point.
  Off-path (`SOLVER_SWAP` unset) must stay bit-identical (gate verification).
- `_EVALS`: a swap evaluation counts as one eval (like a relocation candidate), so the
  eval-count A/B is fair (swap-enabled spends part of the fixed budget on swap candidates).
- `_POOL`: swap-induced bay re-packs record their `(bay, set)` pieces into `_POOL` like any
  other eval -> recombination benefits from the richer pool (a bonus, not a hazard).

## 9. Risks and Controls

- **Budget dilution:** swaps consume part of the fixed eval budget; if they rarely improve,
  the relocation search is starved -> worse at fixed E. Control: top-K guidance keeps swap
  candidates few; run swaps only after relocations stall (second-tier); `SOLVER_SWAP_K`
  tunes the spend.
- **Swap == two relocations the search already reaches:** if relocations already find the
  optimum, swaps add nothing. The eval-count A/B measures exactly this (no gain -> reject).
- **Signal staleness:** exit times are valid for the current incumbent; after a swap the two
  bays re-pack and refresh their exit times (free). Other bays' signals are unchanged.
- **Cross-term weighting (alpha, beta):** instance weights `w1/w2/w3` vary; derive the
  family priority from the actual `w*Z` split rather than fixed alpha/beta where possible.

## 10. Implementation Plan

- **Step A.** Cache per-block exit times: extend the per-bay Z1 cache value from `T` to
  `(T, exited)` (or a parallel map), so the search can read `tard_i` / `slack_i` for the
  current incumbent without extra packing.
- **Step B.** Candidate generators for families A/B/C from the cached signals + `loads` +
  preferences; `top-K` per family; combined ranking.
- **Step C.** `try_swap` / `apply_swap` (full-recompute version first -- correctness over
  speed) and a guided-swap sweep added to `local_search` (gated `SOLVER_SWAP`).
- **Step D.** Quality A/B (eval-count, full-recompute): full-20 at fixed `SOLVER_MAX_EVALS`,
  swap on vs off -> objective delta. This is the verdict.
- **Step E.** If it helps: extend to `_climb`; add a guided-swap Destroy profile; then route
  `try_swap` through the incremental evaluator for wall speed.

## 11. Run Matrix

| run | instances | mode | purpose |
|---|---|---|---|
| signal-sanity | 3 | dump top-K swap candidates mid-search | confirm signals pick sensible pairs |
| quality-K | 5 (hard/med) | `SOLVER_MAX_EVALS=2500`, K in {5,10,20}, on vs off | does swap help at fixed E? tune K |
| quality-full | 20 | `SOLVER_MAX_EVALS=2500`, best K, on vs off | full-20 obj delta (verdict) |
| family-ablation | 20 | families {z1},{z2},{z3},{all} | which family contributes |
| destroy-swap | 20 | guided-swap Destroy as a portfolio profile | diversification value |

## 12. Success Criteria

- **Adopt** (as a neighborhood) if full-20 at fixed eval count improves aggregate by a
  clear margin (e.g. >= 3%) with no severe per-instance regression, all feasible.
- **Portfolio member** if it does not win globally but wins specific instances (like guided
  Destroy / R-diversity) -> add as a swap-Destroy profile rather than a default.
- **Reject** if no eval-count gain (relocations already suffice) or if the budget dilution
  costs more than the swap wins.

## 13. Decision Rules

1. Signal-sanity shows the guided pairs are sensible -> proceed; else fix the signal.
2. quality-K flat or negative at all K -> reject the neighborhood; consider only the
   guided-swap Destroy profile (cheaper test of the same idea).
3. quality-full >= 3% -> extend to `_climb`, then incremental-eval for wall, then adopt.
4. Wins are instance-specific -> ship as a portfolio profile, not a default.

## 14. Notes

- Depends conceptually on, and composes with, the incremental-eval work: swap and
  relocation share the "2 bays changed" delta, so one evaluator serves both.
- The guided-swap Destroy variant is the cheapest first probe of the core idea (a single
  targeted exchange as a kick) and slots straight into the parallel portfolio as a profile.
- Keep the signal honest: it is post-packing positional slack (informative), explicitly not
  input temporal slack (vacuous, per Exp0 in docs/methodology.md).
