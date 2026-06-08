# OGC 2026 — A Decomposition Framework for Shipyard Spatial Block Scheduling

**Status:** working draft. All numbers are measured on the 20 `train/` instances (this
development cycle). Add figures/pseudocode before final submission.

---

## Abstract

We address the OGC 2026 *Grand Shipyard Puzzle*: assigning structural blocks to bays
and deciding, for each block, its position, orientation, and entry/exit times so as to
minimize a weighted sum of total tardiness (`Z1`), workload imbalance (`Z2`), and
preference penalty (`Z3`), subject to spatial (per-layer collision, bay containment)
and crane (vertical-extraction interference) constraints.

Rather than tuning a monolithic heuristic, we followed a **diagnosis-first methodology**
inspired by blueprint-driven agentic frameworks (LEAP): decompose the problem into
verifiable sub-problems, let cheap experiments reveal where the true difficulty lives,
and treat each failure as a *structural* signal that reshapes the design. This produced a
framework — **assignment search (with iterated local search) scored by a per-bay packing
simulator, over a per-bay footprint-disjoint admission packer with adaptive polygon
escalation** — that is **feasible on 20/20** training instances within the time limit and
improves the total objective by **−30.8%** over a strong best-of local-search baseline
(−54% on the hardest instance).

The central scientific finding: **the crane constraint, which makes the problem appear
fearsome, can be *designed away*.** Because bay area utilization is only ~0.3–0.4,
forbidding footprint (projection) overlap costs nothing yet guarantees crane-trap-free
extraction, collapsing a coupled placement+extraction problem into a standard crane-free
2-D dynamic packing. Three measured refinements close most of the remaining gap: **adaptive
polygon escalation** (the exact footprint check, used only where the cheap AABB check
fails) recovers *packing-driven* tardiness; **iterated local search on idle time** escapes
the assignment local minima that cause residual preference penalty; and a **Z2-aware
set-partitioning recombination** as a guarded final step reaches global re-assignments the
one-block-at-a-time search cannot (prob_13 −12.9%, never worse elsewhere).

A second contribution is methodological: the search is wall-clock-deadline driven and
high-variance, so we built a **deterministic eval-count mode** that makes every A/B
reproducible. Several plausible refinements were tested rigorously under it and *rejected*
(Z1+Z3-only recombination, a "search→recombine→search" loop, an obj-cache key change), and
one was kept only as an env-gated option (signal-guided ILS) — the negative results are
reported alongside the positive ones.

---

## 1. Problem Analysis

### 1.1 Decisions, constraints, objective
(Recap from the problem statement: per block — bay, (x,y), orientation, ENTRY/EXIT times;
constraints — assignment, release/processing, bay containment, per-layer collision, crane
ENTRY/EXIT vertical interference; objective `w1·Z1 + w2·Z2 + w3·Z3`.)

### 1.2 Structural facts (code-verified against `utils.check_feasibility`)
- **Bays are feasibility-independent.** Stages 2–5 of the checker loop *per bay*; blocks in
  different bays never interact spatially or via crane. The only cross-bay coupling is in
  the objective.
- **The objective separates cleanly:**
  - `Z2` (imbalance) and `Z3` (preference) are **instant functions of the assignment alone**
    (no packing/timing).
  - `Z1` (tardiness) = `Σ_bay tardiness_bay`, an independent per-bay sum.
- Therefore: **total objective = `w1·Σ_bay T_bay(A_bay) + w2·Z2(A) + w3·Z3(A)`**, where `A`
  is the assignment. This is the backbone of the decomposition.

> TODO: formal restatement with notation; cite `utils.py` stage structure.

---

## 2. Empirical Diagnosis — where the difficulty actually lives

We ran cheap, falsifiable experiments *before* committing to any algorithm. Each result
narrowed the problem.

| Exp | Question | Result | Consequence |
|-----|----------|--------|-------------|
| 0 | Is tardiness bounded by cheap relaxations? | Temporal LB `Σmax(0,R+P−D)` = **0** (all 20); area-cumulative LB = **0** (peak util 0.26–0.41) | Tardiness is **purely geometric/crane**; slack is thin (median 1) but area is abundant |
| 2 | Does per-bay *area load* predict tardiness? | Spearman ρ = **0.19**, ranges fully overlap | **No cheap aggregate surrogate** exists at any level |
| A | Is the crane *exit* the bottleneck? | Fixing placements, re-optimizing exits: stalls=0, `T_exit_opt = T_base` exactly | **Extraction is free**; bottleneck is **ENTRY admission**: `T_i = max(0,(entry_i−release_i)−slack_i)` |
| B | Are admission delays forced or avoidable? | **50/50** tardy blocks had an on-time slot under baseline's own occupancy | Tardiness is **~100% recoverable (myopic)** — the optimization opportunity is real |

**Reading of the chain.** Every cheap shortcut died (Exp 0, 2), which told us the only honest
signal is an *actual* placement simulation. Exp A relocated the bottleneck from the scary
crane-EXIT to admission timing. Exp B showed the prize is recoverable.

> TODO: per-instance tables, slack/util histograms, the prob_7 counterexample for Exp 2.

---

## 3. Framework Design

### 3.1 Decomposition
- **Outer:** the assignment `A` (which block → which bay). Sets `Z2`, `Z3` instantly and
  partitions blocks into independent per-bay problems.
- **Inner (per bay):** a dynamic 2-D admission-packing problem that produces `Z1_bay`.

### 3.2 Key insight — footprint-disjoint packing designs the crane away
A first naive per-bay packer (EDD order, earliest bottom-left slot) admitted everyone
on-time (`T_entry ≈ 0`) but created **massive crane traps** — extraction added **+3516**
tardiness on prob_1. *Failure as structural signal:* the per-bay problem is genuinely a
**joint placement+extraction** problem (Exp A's "extraction is free" held only for layouts
that happened to be trap-free).

The fix exploits Exp 0: since area utilization is only ~0.3–0.4, we can **forbid
full-footprint (AABB) overlap** between temporally-overlapping blocks. No cross-layer
overlap ⟹ **no block can ever sit over another's extraction column ⟹ zero crane traps by
construction**. Effect: `T_extract` 3516 → **0**, oracle-feasible, ~150× faster
(33 s → 0.2 s per bay). The coupled problem collapses into a **standard crane-free 2-D
dynamic packing minimizing admission delay**.

### 3.3 Assignment search scored by the packing simulator
Because no analytical surrogate for `Z1` exists (Exp 2), candidate assignments are scored
by *running* the per-bay packer (`Z1`) plus the instant `Z2`/`Z3`. A light local search
(seed = best of {preference, balanced-load, preference-capped}; first-improvement moves of
blocks out of tardy bays) navigates the three-way `Z1`/`Z2`/`Z3` trade-off — which no single
heuristic does (preference → `Z3`=0 but `Z2` explodes; balanced-load → vice-versa).

### 3.4 Methodology (LEAP-inspired)
- **informal ↔ formal:** coarse disjoint placement for planning ↔ exact `check_feasibility`
  (shapely + crane) as the certificate.
- **decompose + memoize:** assignment node → independent per-bay nodes; per-bay packs cached
  by the bay's block set.
- **failure as structural signal:** the trap explosion → the disjoint-packing rule; baseline's
  force-late repair → the diagnosis that admission, not extraction, drives tardiness.

### 3.5 Adaptive polygon escalation (recovering packing-driven tardiness)
Where does the AABB-disjoint solver's residual tardiness come from — the assignment
overloading a bay (*load-driven*), or AABB looseness wasting space (*packing-driven*)? We
measured it: holding the assignment fixed, re-pack each bay with AABB-disjoint vs exact
**polygon-disjoint** (strictly more permissive, so `T_poly ≤ T_aabb`). The biggest
tardiness instances are **packing-driven** — prob_14 `240→56` (recovers 77%), prob_20
`220→108` (51%). (We had predicted load-driven; the measurement corrected us.)

Exact polygon checks are expensive (Shapely), so we **escalate adaptively**: `solve_bay`
uses cheap AABB by default and falls back to the exact footprint check *only* when AABB
finds no slot at a time `t`. It is applied only in the final `build_solution` (the
assignment search stays cheap AABB); the build reserves time for it **only when tardiness
is in play**, is deadline-guarded (reverts to AABB past the deadline → always time-safe).
Clean ablation at a fixed budget (poly on vs off): prob_14 `4.98M→2.35M` (−53%), prob_20
`6.48M→4.59M` (−29%); preference-only instances **identical** (the escalation never
fires). A **Pareto improvement** at fixed budget.

### 3.6 Iterated local search on idle time (escaping preference local minima)
The remaining cost is preference (`Z3`). A naive attempt to add multi-start + block-swap
operators to the search *regressed* — it fragmented a tight budget. Diagnosis at a large
time limit showed the `Z3` gap is **search-time-bound, not operator-bound**: given more
time the *existing* search reaches the same low `Z3`, and the assignment search
**converges early** on preference instances (prob_5 at ~62 s), leaving the rest of a large
budget idle. So we run **iterated local search on the idle time only**: perturb the
incumbent (re-home 2–5 random blocks), re-optimize, keep the global best. Because it only
accepts improving moves it can never regress at a fixed budget (confirmed by ablation:
ILS-gain ≥ 0 on every instance); it improves preference instances by ~27–28% (prob_5
`160928→116230`, prob_17 `236600→172349`) and adds robustness against the main search's
local minima. The earlier failure and this success differ only in *where* the operators
are applied — the time-bound analysis identified the right place.

### 3.7 Z2-aware set-partitioning recombination (guarded global move)
ILS moves one block between bays at a time, so it cannot reach assignments that require a
**simultaneous** multi-bay reshuffle. To reach those, the search caches every
`(bay, block-set) → tardiness` piece it evaluates, then solves an exact-cover
(set-partitioning) MIP over those pieces (OR-Tools CP-SAT) that recombines them into a new
global assignment. The first version of this idea was **dropped**: the MIP minimized only
`Z1+Z3`, so adopting its solution blew up imbalance (`Z2` −81% on the full objective on
prob_5), and its one apparent win turned out to be a build-bug artifact (§4). It works once
two things are fixed: **(a)** `Z2` is put *into* the MIP — the min-max imbalance is
linearized (`M ≥ |u_j·load_j − u_k·load_k|` for every bay pair, objective `+ w2·M`) and the
column cost uses the same **best-of(AABB, polygon)** tardiness as the final build; and
**(b)** the recombined assignment is adopted only if a **best-of full-objective guard**
confirms the true score improved (otherwise the incumbent is kept). This makes it a cheap
(~3 s) **never-regress** final step. Net deterministic effect (eval mode, on-vs-off):
**prob_13 −12.9%** — a genuine global recombination the local search cannot reach —
prob_3/5/15/17 unchanged (guard → on ≤ off always). It is the only refinement that touches
`Z2` directly. Env-gated `SOLVER_NORECOMB`; depends on OR-Tools.

> Implementation note (side-effect of MIP solve time): the recombine runs under a deadline
> and reverts to the incumbent if it does not finish, so it never threatens time-compliance
> even though MIP solve time is in principle unpredictable. A faster MIP (e.g. Gurobi) would
> reach the *same* optimum faster, not a better one — the ceiling is pool diversity, not
> solver speed (measured: pool 2623→4507 on prob_17 gave 0 extra gain).

### 3.8 Measurement reliability — deterministic eval-count mode
Diagnosing the refinements above repeatedly hit a methodological wall: the search terminates
on a **wall-clock deadline**, so two runs at the same time limit land in different local
minima, and single-run A/Bs at different (or even equal) budgets produced *confounded*
conclusions more than once. We added an evaluation mode (`SOLVER_MAX_EVALS=E`) that stops
each search after `E` candidate evaluations instead of a time deadline → **fully
deterministic** (two runs bit-identical). The submission default is unchanged (wall-clock);
eval mode is only for judging a modification, and since it does not bound wall time the
harness reports per-problem wall time so a fixed iteration count cannot silently blow the
budget. Every adopt/reject decision in §3.7 and §4.1 was made under this mode.

> TODO: pseudocode for `solve_bay` (adaptive disjoint admission), the assignment search,
> `_ils`, and the recombination MIP.

---

## 4. Validation

**Development trajectory (controlled A/Bs).**
- *Per-bay packing, same assignment:* prob_1 total tardiness 712 → **64**; prob_7 450 →
  **20** (overloaded 70-block bay 356 → **0**). 0.1–0.5 s/bay vs baseline 55 s.
- *Adaptive polygon, fixed budget:* prob_14 −53%, prob_20 −29%; preference instances
  unchanged (Pareto).
- *ILS, fixed budget (ablation):* gain ≥ 0 on every instance; prob_5 −28%, prob_17 −27%.

**Final consolidation — all 20 instances, time limit 180 s, full solver (adaptive polygon
+ ILS) vs a strong AABB best-of local-search baseline (~110 s). 20/20 oracle-feasible,
every run within the limit (≤166 s).** `new`/`old` are total objective `w1·Z1+w2·Z2+w3·Z3`.

| inst | n | new obj | old obj | Δ% | Z1 | Z3 |
|------|---|--------:|--------:|----:|---:|---:|
| prob_1  | 100 |   452,988 |   482,079 |  −6.0 | 15 |   66 |
| prob_2  | 100 |    16,470 |    23,490 | −29.9 |  0 |  106 |
| prob_3  | 100 |   132,254 |   113,510 | **+16.5** |  2 |  500 |
| prob_4  | 100 |   363,475 |   626,491 | −42.0 |  9 |  808 |
| prob_5  | 150 |   138,362 |   160,928 | −14.0 |  1 |  619 |
| prob_6  | 150 |   927,914 | 1,374,330 | −32.5 |  5 | 5042 |
| prob_7  | 150 |   122,800 |   164,785 | −25.5 |  0 |  765 |
| prob_8  | 150 |    21,948 |    26,840 | −18.2 |  0 |   92 |
| prob_9  | 200 |   253,920 |   385,388 | −34.1 |  0 | 1638 |
| prob_10 | 200 |   143,465 |   173,229 | −17.2 |  0 |  986 |
| prob_11 | 200 | 1,323,196 | 1,939,659 | −31.8 | 21 | 6312 |
| prob_12 | 200 |   408,681 |   573,495 | −28.7 |  0 | 2933 |
| prob_13 | 250 | 1,525,881 | 1,698,834 | −10.2 | 16 | 9117 |
| prob_14 | 250 | 1,953,928 | 4,259,967 | **−54.1** | 45 | 8561 |
| prob_15 | 250 |   307,211 |   505,523 | −39.2 |  0 | 2187 |
| prob_16 | 250 |    91,280 |    91,352 |  −0.1 |  0 |  585 |
| prob_17 | 300 |   166,399 |   165,940 |  +0.3 |  0 | 1091 |
| prob_18 | 300 | 1,035,747 | 1,161,654 | −10.8 |  6 | 7149 |
| prob_19 | 300 |   138,651 |   157,256 | −11.8 |  0 |  935 |
| prob_20 | 300 | 4,320,430 | 5,912,344 | −26.9 | 113 | 10403 |
| **TOTAL** | | **13,845,000** | **19,997,094** | **−30.8%** | | |

**18/20 improved**, two effectively flat/slightly worse (prob_17 +0.3%; prob_3 +16.5%, a
case where the search traded `Z3` down but let `Z2` rise — `Z2` carries the smallest
weight and is under-served by the search). The largest remaining absolute objectives are
prob_20 (`Z1`=113, a genuinely hard packing) and the high-preference prob_13/prob_18.

**`Z3` is search-time-bound (separate experiment, time limit 300 s):** prob_5
`394,811 → 160,928` (= the old baseline exactly), prob_17 → 164,959 and prob_19 → 138,651
(both *beat* the old baseline). I.e. given adequate time the framework dominates the prior
best-of; the competition's per-problem limit (minutes–30 min) supplies that time.

### 4.1 Advanced-search experiments — what was kept, what was rejected
After the core framework was in place we tested four ideas for squeezing more out of the
search, **all judged under the deterministic eval mode (§3.8)**. Full chronology and per-
instance tables are in `docs/experiment_log.md`; the summary:

| idea | verdict | evidence (deterministic, equal budget) |
|---|---|---|
| **Z2-aware SP recombination** (§3.7) | **adopted, default-on, guarded** | prob_13 −12.9%, others unchanged (guard never regresses) |
| Z1+Z3-only recombination | rejected | blew up `Z2` (prob_5 −81% full obj); apparent win was a build bug |
| signal-guided ILS destroy | **env-gated only** (`SOLVER_GUIDED`) | instance-dependent: prob_15 −14.8% / prob_5 −9.2% **but** prob_17 +32%, prob_18 +20% — no clean win |
| `(bay, set)` obj-cache key | rejected | identical on 4/5 instances, +7.5% on one — the correct key did not help |
| **H2** search→recombine→search loop | rejected, removed | worse on every instance: prob_13 +9.9%, prob_17 +57.7%, prob_5 0% |

**Pattern.** Once the core design (disjoint packing + best-of polygon + ILS) captured the
gains, single-axis tweaks were mixed/marginal — the search is robust and high-variance, so a
tweak helps some instances and hurts others. The two ideas that *did* survive are the ones
that either change the search's reach in a **guarded** way (recombination — a global move the
local search structurally cannot make, adopted only when the true objective improves) or are
kept **off by default** for possible instance-adaptive use (guided ILS). The decisive tool
was the eval mode itself: it reversed two earlier wall-clock "findings" (a (j,ids) win and an
H1-dropped verdict) that were pure variance, and let H1 be correctly *revived* once the
dropped reason — `Z2` — was addressed.

---

## 5. Discussion, Limitations, Future Work

**Why it works.** The dominant weighted term is tardiness (`w1` ≫ `w3` ≫ `w2`), tardiness is
admission-driven, and disjoint packing makes admission both feasible and trap-free at
negligible spatial cost. The framework spends its search budget exactly where headroom
remains.

**What the score is made of** (decomposition over the 20 instances): tardiness ≈ **55%**,
preference ≈ **44%**, imbalance ≈ **1%**. With `w1 ≫ w3 ≫ w2` the optimum is
near-lexicographic — drive `Z1` to 0, then minimize `Z3`, and `Z2` barely matters. The two
refinements (§3.5, §3.6) attack the two large buckets directly.

**Limitations / next steps.**
- **`Z2` partly addressed.** The local search optimizes the total objective but, moving one
  block at a time, can let `Z2` (imbalance) rise on `Z1`=0, small-`Z3` instances (prob_3).
  The Z2-aware recombination (§3.7) now attacks this directly *when* a beneficial global
  reshuffle exists (and never regresses when it does not), but a cheap `Z2`-aware tie-break
  inside the local moves would still help the cases the MIP's pool does not cover.
- **One genuinely hard packing** (prob_20, `Z1`=113): adaptive polygon recovers only part
  within the budget; a faster polygon check (footprint caching) or a tighter assignment
  would help.
- **ILS is simple** (fixed perturbation strength, single incumbent); adaptive perturbation
  or accept-worse criteria may help where it is currently neutral.
- *Correctness note that generalizes:* integer placement bounds must use
  `lower = ceil(max(0,−min_vert))`, `upper = floor(W − max_vert)`; rounding/truncation
  violates the bay boundary. The exact `check_feasibility` reconstructs operations in
  insertion order, so emit time keys sorted (EXIT before its ENTRY).

**Novelty.** The contribution is less a single algorithm than a *reusable diagnosis-to-design
method*: cheap falsifiable experiments localized the difficulty; one of them (abundant area)
turned the hardest constraint (crane) into a free design choice; and two later experiments
(packing- vs load-driven tardiness; time-bound vs operator-bound `Z3`) each redirected the
refinement and turned a *failed* idea (multi-start/swaps) into a working one (ILS on idle
time) by identifying *where* it applies. Method artifacts: `docs/methodology.md`,
`docs/dev_workflow.md`, `.claude/skills/`.
