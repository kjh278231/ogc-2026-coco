# Experiment Log — advanced search & measurement reliability

Chronological record of the hypothesis-driven experiments after the core framework
was in place (see `docs/technical_report.md` for the earlier Exp 0–B diagnosis).
Negative results are kept on purpose. All objectives are the scored
`w1·Z1 + w2·Z2 + w3·Z3` on the 20 `train/` instances; lower is better.

> **Meta-lesson that runs through this log:** the search is wall-clock-deadline
> driven, so two runs at the same time limit land in different local minima. Single
> run A/B at different/equal time is **unreliable** and repeatedly produced
> confounded conclusions. Compare at the **same** time limit, and prefer the
> deterministic **eval-count mode** (below) for judging a modification.

---

## Hypotheses under test
Three advanced-search ideas proposed on top of the working framework:
1. **Bay-subset candidate pool + global recombination** — store every
   `(bay, block_set) → tardiness` piece the search evaluates, then pick a global
   exact-cover (set-partitioning MIP) of the best pieces.
2. **search → recombine → search loop** — feed the recombined assignment back as a
   seed and re-optimise.
3. **Smarter destroy-repair perturbation** — replace random ILS perturbation with
   objective-signal-guided destroy + ordered repair.

H2 depends on H1, so H1's premise was tested first; H3 is independent.

---

## H1 — set-partitioning recombination

**Premise test** (collect pieces from a normal run, solve the exact-cover MIP with
OR-Tools CP-SAT, compare to the search's best, same AABB basis):

| instance | pool size | recombination gain (Z1+Z3) |
|---|---|---|
| prob_3 | 4949 | **+12.8%** |
| prob_5 | 2653 | +1.1% |
| prob_13 | 383 | 0% |
| prob_17 | 2623 | 0% |

Richer pools did **not** create gains on the zero-gain instances (prob_17 pool
2623→4507 at a larger budget, still 0%). The MIP reaches OPTIMAL in ≤4 s, so the
ceiling is set by **pool diversity, not solver speed** — a faster MIP (e.g. Gurobi)
would reach the *same* optimum faster, not a better one. **Verdict: the ILS search
already recombines its own pieces near-optimally; recombination is marginal.**

**The one apparent win was an artifact.** Probing prob_3's full objective showed
SP 83,000 vs incumbent 132,254 — but a debug trace revealed the incumbent's AABB
total was 78,920 (better than SP). The 132,254 came from the *polygon build of the
incumbent* being worse than its AABB build. So recombination's "win" was masking a
**build bug**, not beating the search. The recombination integration was removed.

---

## Bug found via H1 — polygon build can be worse than AABB (fixed)

Polygon escalation is more permissive per block, but packing is **greedy**: placing
one block earlier (a tighter polygon fit) can push later blocks out, so polygon
packing can give **worse total tardiness** than AABB for the same bay (prob_3: AABB
Z1=0, polygon Z1=2). `build_solution` had forced polygon.

**Fix (committed 32429ca):** per-bay **best-of(AABB, polygon)** — pack both, keep
the lower tardiness. By construction never worse than either. Clean same-tl(180)
check vs polygon-only: prob_20 4,320,430 → **4,267,096** (−53k, T 113→111);
prob_3/5/14 unchanged. Never worse.

---

## Measurement reliability — deterministic eval-count mode (committed 7d63719)

Diagnosing the above kept hitting wall-clock variance. Added an evaluation mode:
`SOLVER_MAX_EVALS=E` stops the searches after E candidate evaluations (`total_obj`
calls) instead of a time deadline → **fully deterministic** (two runs identical:
prob_3/prob_5 at E=300 matched exactly). The submission default is unchanged
(wall-clock). Eval mode does **not** bound wall time (~0.07 s/eval; tl=60 ≈ 940
evals for prob_5), so the harness reports per-problem wall time.

---

## (j,ids) obj1-cache key — investigated, rejected

The obj1 cache is keyed by block-set only; the same set in different bays returns
the wrong (contaminated) tardiness. The correct key is `(bay, set)`. A noisy full-20
run suggested big swings (prob_1 −32%, prob_4 +54%). The **deterministic** eval-mode
A/B (same E=2000) settled it:

| instance | ids | (j,ids) | Δ |
|---|---|---|---|
| prob_1 | 325,223 | 325,223 | 0 |
| prob_4 | 402,111 | 402,111 | 0 |
| prob_6 | 752,120 | 752,120 | 0 |
| prob_15 | 198,747 | 213,562 | **+7.5%** |
| prob_17 | 216,791 | 216,791 | 0 |

**Verdict: not adopted.** Identical on 4/5, worse on 1 — the full-20 swings were
pure variance. Contamination is rare and the heuristic is robust to it; the correct
key does not improve (and occasionally hurts) search quality. (This is also the
cleanest demonstration of the eval-mode tool's value.)

---

## H3 — signal-guided ILS destroy (env-gated, not default)

Replace random ILS perturbation with destroy of *contributing* blocks (in a tardy
bay, the max-(u·load) bay, or off their preferred bay). Deterministic eval-mode A/B
(E=2000), `SOLVER_GUIDED=1` (always guided) and `=mix` (50/50) vs default random:

| instance | random | guided | mix |
|---|---|---|---|
| prob_5 | 114,920 | **−9.2%** | −0.5% |
| prob_13 | 1,155,273 | **−9.3%** | +7.0% |
| prob_15 | 246,421 | **−14.8%** | −5.7% |
| prob_17 | 240,624 | **+32.3%** | 0% |
| prob_18 | 653,728 | **+20.1%** | 0% |

**Verdict: instance-dependent, no clean win.** Pure guided over-focuses on the same
blocks → low diversity → big wins on 3, big losses on 2. The 50/50 mix avoids the
losses but dilutes the wins (even regresses prob_13). Kept as an env-gated option
(`SOLVER_GUIDED`, default unchanged = random) for possible instance-adaptive use.

---

## Net outcome of this cycle
- Kept: **best-of build fix** (32429ca), **deterministic eval mode** (7d63719),
  guided-ILS as an env-gated option (default unchanged).
- Rejected as defaults: set-partitioning recombination (H1/H2), (j,ids) cache,
  guided/mix ILS.
- **Pattern:** all four advanced-search tweaks are mixed/marginal — the core design
  (disjoint packing + best-of polygon + ILS) already captured the gains, and the
  search is robust + high-variance, so single-axis tweaks help some instances and
  hurt others. Low-hanging fruit is exhausted; further gains need a different angle
  or instance-adaptive strategy selection.
- Measurement is now reliable (eval mode) — the durable basis for any further work.
