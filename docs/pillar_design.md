# 7th solver design log — PILLAR (CG, falsified) → ACCORD (price consensus)

> **Falsification outcome (07-04, §4 gate):** CG KILLED by 4 probes (T17/T13 converged,
> T6+Neame-smoothing-0.7, T13 unconverged E600). Pricing finds negative-RC columns every
> round — up to **RC −231k on a 199k objective** — yet the LP moves **+0.00%** and the
> restricted-master IP / true objective improve **0** in every setup. Mechanism (structural,
> not a bug; RC formula self-checked): the master partitions ~250 blocks into only m≈4-5
> huge overlapping columns, so a new column can enter only if the pool covers its exact
> complement — measure-zero. CG's sweet spot (many small columns) is inverted here.
> **Pivot:** coordination must exchange WHOLE assignments → ACCORD, §6 below.
> See memory/cg-column-pricing-falsified.md.

# (falsified design kept for the record) PILLAR — Price-directed column generation

**Goal (user, 07-04):** a genuinely NEW model — not a best-of of the existing six — that
rethinks the decisive stages of the problem and applies recent research.

## 1. Decisive-stage analysis: what ALL six solvers share

Every existing solver answers the same three questions with the same *paradigm*:

| decisive stage | BRIDGE/PRISM/STOW/WEAVE/FLUX/HELM answer | structural limit |
|---|---|---|
| S1. 배정 (block→bay) | move-level local search (relocate/swap/eject) over one incumbent, or greedy construction (FLUX), or population PR (WEAVE) | reaches only assignments connected to the seed by single moves; multi-bay simultaneous reshuffles need recombine |
| S2. 적치 (per-bay packing → Z1) | packer as a *scorer* of candidate assignments | packer output is consumed as a number; its *marginal value structure* (which block hurts which bay) is thrown away |
| S3. 전역 재조합 | set-partitioning IP over `_POOL` = pieces the search *happened to visit* | **measured ceiling = pool diversity** (tech report §3.7: pool 2623→4507 on T17 → 0 extra gain; cross-worker union → 0) |

The one global tool (recombine) is **passive**: it can only recombine what local search
visited. Nobody *generates* columns aimed at what the global model is missing.

## 2. The PILLAR paradigm: make column generation ACTIVE

Classic Dantzig-Wolfe view of this problem (exactly matches the objective separation
`w1·Σ_bay T_bay + w2·Z2(A) + w3·Z3(A)`):

- **Master**: set-partitioning over columns = (bay j, block-set S) with cost
  `w1·T_j(S) + w3·pl(S,j)` + linearized Z2 coupling — *already implemented* as `_recombine`.
- **Pricing** (the missing half): given LP duals π_i (block cover), μ_j (bay convexity),
  η_ab (Z2 rows), find per bay j the set S minimizing reduced cost
  `w1·T_j(S) + w3·pl(S,j) + g_j·wl(S) − Σ_{i∈S} π_i − μ_j`.
  Everything is linear per block except `T_j(S)` → a **prize-collecting packing problem**
  with the existing numba packer as the T-oracle.

The duals answer *"which block is globally under/over-served, and at what price"* — a signal
no local move sees. Pricing turns the packer from a scorer into a **column generator**.

### Loop (deadline-driven, guarded)

1. Seed pool: diverse constructives (a_pref / a_balanced / a_pref_capped) + short LAHC burst
   to fill `_POOL` with packing-realistic pieces.
2. Restricted master **LP** (Gurobi, float costs) → duals; **Wentges/Pessoa dual smoothing**
   (α adaptive) to fight set-partitioning degeneracy.
3. **Heuristic pricing** per bay (greedy add by profit density + drop pass; seeds: ∅,
   incumbent bay-set, best-priced-so-far). Add all columns with RC < −ε.
4. Every K rounds / at deadline: restricted-master **IP** (8s cap, incumbent MIP start)
   → candidate assignment; adopt via the existing **true-objective guard** (Pareto-safe).
5. Optional short repair on the adopted assignment (Z1-only local fix), then back to 2.

### Safety properties (inherited from repo discipline)

- Incumbent's own columns always in the master → IP ≥ incumbent representable → guard can
  never regress (same invariant as `_recombine`).
- Single process → single Gurobi env → no license contention ([[gurobi-single-use-license]]).
- Deadline-guarded LP/IP caps; fallback = pool-only recombine = shipped behavior.

## 3. Recent research applied

- **CG-based matheuristics / price-and-branch**: solving the restricted master as IP over
  generated columns is a state-of-practice heuristic for set-partitioning-structured
  problems (EJOR 2025 inventory-routing CG matheuristic; MPC 2023 "novel pricing scheme for
  high-quality solutions in set covering/packing/partitioning").
- **LNS-generated columns**: recent matheuristics use LNS/heuristics *as the pricer* when
  exact pricing is intractable (EJOR 2025) — exactly our packer-oracle pricing.
- **Dual stabilization**: Wentges (1997) smoothing with Pessoa et al. auto-adaptive α;
  2026 work (arXiv 2604.23889 "Learning to Control Stabilization in CG") confirms smoothing
  control is the decisive convergence lever in degenerate masters.
- **Degenerate set-partitioning acceleration** (arXiv 2604.12070): equality masters are
  highly degenerate → smoothing + interior duals matter more than pricing exactness.

## 4. Falsification gate (cheap-falsification, run BEFORE building)

Hypothesis: dual-guided pricing finds neg-RC columns beyond the pool AND they improve the
restricted-master IP's true objective on instances where pool-growth alone was proven
useless (T17), plus recombine-active instances (T13, T6).

Probe: `.claude/scratch/_cg_probe.py` — E=2000 deterministic search → pool; LP relax of the
exact recombine model; RC formula self-checked against pool columns (min RC ≥ 0 at LP opt);
greedy+drop pricing; verdict = IP(pool) vs IP(pool+priced) true objectives.

- **Kill condition**: ~0 neg-RC columns, or 0 true-objective improvement everywhere.
- **Build condition**: neg-RC columns exist and IP(pool+priced) < IP(pool-only) on ≥1
  instance (esp. T17) without regression elsewhere.

## 5. Planned architecture (if gate passes)

```
pillar/
  myalgorithm.py     # entry: env defaults, timelimit routing, feasible fallback (a_pref build)
  pillar_engine.py   # CG loop: master LP + smoothing + pricing + periodic IP dive + guard
  (bridge kernel reused: packing.py oracle, solver.py scoring/build/caches — infrastructure,
   not best-of: no BRIDGE/PRISM search runs inside PILLAR)
```

Budget layout (T total): seed ~15% → CG rounds ~65% (LP ms-scale, pricing dominates; oracle
calls are the same numba packer the search uses per eval) → final IP + build reserve ~20%
(reuse `_score_and_pack` reserve discipline).

---

## 6. ACCORD — the pivot that passed (7th solver, implemented)

**Model:** per-block congestion-price consensus. Iterate
`{price-augmented assignment MIP (lam·w2·Z2 + w3·Z3 + Σ price[i,j]·x[i,j], exact, <1s)
 → true per-bay pack (multi-order + mask kernel as oracle)
 → tatonnement price update (decay 0.7; tardy block i charges price[i,bay] += ρ·w1·τ_i)}`
keep the best true-model iterate (a_pref floor), materialize with the validated builder.

- ADMM / surrogate-Lagrangian / feasibility-pump family: alternating projection between
  the preference-ideal polytope (exact MIP) and the packable set (packer oracle), coupled
  by prices. PRISM's static-λ anchors are the no-feedback special case.
- The CG falsification is what shaped it: coordination must exchange WHOLE assignments
  (columns strand). Prices are per-BLOCK (dense n-dim signal), sidestepping FACET's
  per-SET cut explosion (whack-a-mole falsified earlier).

**Probe gate (07-04, `.claude/scratch/_accord_probe.py`, PASS):** vs an equal-budget
static-λ grid {0.25..1024} (PRISM-anchor family), raw price-loop iterates (NO search/
repair) win 3.9–4.8×:

| inst | static grid best | price loop best | note |
|---|---|---|---|
| T13 | 955,949 (λ=64, 11s) | **200,288** (2s, 10 iters) | Z1 94→0 by it4; osc. after it6 |
| T20 | 2,103,329 (λ=64, 16s) | **543,671** (5s, 10 iters) | Z1 261→9 by it3 |

Oscillation (tatonnement overshoot) is the visible next lever → engine adds adaptive ρ
(×0.6 on worsening / ×1.05 on improving), incumbent backtrack (warm start → best after a
>1.3× blow-up), and seeded multiplicative price jitter after 6 stalled iters.

**Implementation:** `accord/accord_engine.py` (+ `accord/myalgorithm.py` entry;
`tools/_accord_run.py` runner). Kernel reuse = packer/scorer/builder only (like PRISM);
no LAHC/ILS/recombine inside — the price loop IS the search. Determinism knob:
`ACCORD_ITERS` (fixed iteration budget; single-thread seeded MIPs, deterministic packer).
