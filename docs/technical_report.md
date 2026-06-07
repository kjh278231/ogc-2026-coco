# OGC 2026 — A Decomposition Framework for Shipyard Spatial Block Scheduling

**Status:** working draft / skeleton. Numbers below are from the 20 `train/` instances
(measured this development cycle); expand prose and add figures before submission.

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
and treat each failure as a *structural* signal that reshapes the design. This produced
a framework — **assignment search scored by a per-bay packing simulator, over a per-bay
footprint-disjoint admission packer** — that is feasible on **20/20** training instances
within **≤52 s**, keeps tardiness low even at *n*=300, and beats the organizer baseline
on every head-to-head instance tested.

The central scientific finding: **the crane constraint, which makes the problem appear
fearsome, can be *designed away*.** Because bay area utilization is only ~0.3–0.4,
forbidding footprint (projection) overlap costs nothing yet guarantees crane-trap-free
extraction, collapsing a coupled placement+extraction problem into a standard crane-free
2-D dynamic packing.

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

> TODO: pseudocode for `solve_bay` (disjoint admission) and the assignment local search.

---

## 4. Validation

- **Per-bay A/B (same assignment, packing only):** prob_1 total tardiness 712 → **64**;
  prob_7 450 → **20** (overloaded 70-block bay 356 → **0**). 0.1–0.5 s/bay vs baseline 55 s.
- **Full objective vs baseline (6 instances):** framework wins **6/6** — prob_2 ×54,
  prob_3 ×2.3, prob_5 ×16, prob_7 ×18; prob_1/prob_8 produce feasible solutions where the
  baseline is infeasible.
- **Robustness (all 20 train instances):** **20/20 oracle-feasible**, runtime **1–52 s**
  (well within the competition's minutes-to-30-min limit). `Z1` stays low even at *n*=300
  (prob_17/18/19: 0/9/4; worst prob_14 = 232).

> TODO: full 20-row results table; baseline numbers on all 20 for a complete comparison.

---

## 5. Discussion, Limitations, Future Work

**Why it works.** The dominant weighted term is tardiness (`w1` ≫ `w3` ≫ `w2`), tardiness is
admission-driven, and disjoint packing makes admission both feasible and trap-free at
negligible spatial cost. The framework spends its search budget exactly where headroom
remains.

**Limitations / next steps.**
- Residual cost is now `Z2`/`Z3` (esp. preference) — a **stronger assignment search**
  (multi-start, swaps, simulated annealing) is the main remaining lever.
- Strict time-budget management (current build overshoots the budget by 1–2 s).
- Relaxing strict AABB-disjoint → polygon-disjoint to reclaim space only when a bay is
  crowded (not currently needed on training instances).
- A correctness note that generalizes: integer placement bounds must use
  `lower = ceil(max(0,−min_vert))`, `upper = floor(W − max_vert)`; rounding/truncation
  violates the bay boundary.

**Novelty.** The contribution is less a single algorithm than a *reusable diagnosis-to-design
method*: cheap falsifiable experiments localized the difficulty, and one of them (abundant
area) turned the hardest constraint (crane) into a free design choice.
