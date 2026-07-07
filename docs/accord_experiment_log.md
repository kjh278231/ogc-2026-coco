# ACCORD experiment log (7th solver — per-block congestion-price consensus)

> Chronology + numbers for the 7th-solver track. Design + falsification chain:
> `docs/pillar_design.md`. Long-lived conclusions: `memory/cg-column-pricing-falsified`,
> `memory/accord-price-consensus`.

## 07-04 — PILLAR (column generation) falsified before building

Probe `.claude/scratch/_cg_probe.py` (pool from a deterministic E-eval search → LP-relaxed
recombine master → duals [RC formula self-checked: min pool RC = 0 at LP opt] → prize-
collecting per-bay pricing with the packer as T-oracle → IP(pool) vs IP(pool+priced)).

| setup | neg-RC cols | best RC | LP move | IP/true improvement |
|---|---|---|---|---|
| T17 E2000 (pool-ceiling instance) | 32 | — | +0.00% | 0 |
| T13 E2000 | 40 | — | +0.00% | 0 |
| T6 E2000 + Neame smoothing α=.7 | 39 | — | +0.00% | 0 |
| T13 E600 (unconverged, 45% gap to converged) | 60 | **−231,827** (obj 199k!) | +0.00% | 0 |

Diagnosis: **column stranding.** m≈4-5 huge overlapping columns; a new column enters the
LP only if the pool covers its exact complement — never true for single-trajectory pools.
Smoothing + LP-fractional seeds don't help; the failure is structural (CG's sweet spot =
many small columns — inverted here). Side-finding: pool-only recombine at the UNCONVERGED
E600 incumbent also improves 0 → single-trajectory pools are all near-incumbent; historical
recombine wins need cross-basin pools.

## 07-04 — ACCORD probe PASS (whole-assignment price loop)

Probe `.claude/scratch/_accord_probe.py`: {price-augmented assignment MIP → true pack →
price update (decay .7, charge tardy blocks ρ·w1·τ)} vs equal-budget static-λ grid
{0.25..1024} (= PRISM-anchor family, no feedback). Raw iterates, no search/repair:

| inst | static grid best | price loop best | Z1 path |
|---|---|---|---|
| T13 | 955,949 (λ=64, 11s) | **200,288** (2s/10 it) | 94→0 by it4 |
| T20 | 2,103,329 (λ=64, 16s) | **543,671** (5s/10 it) | 261→9 by it3 |

Failure mode identified: tatonnement overshoot/oscillation after ~it6.

## 07-04 — engine v1 + damping sweep (T13@60, wall, single runs)

`accord/accord_engine.py` (adaptive ρ ×0.6/×1.05, backtrack >1.3×, stall-6 jitter,
a_pref floor, poly reserve discipline, `ACCORD_ITERS` determinism knob).
Control: PRISM(+MO+SWAP) 4-core portfolio @T=60 = **114,810** (Z1=0).

| config | obj | Z1 | verdict |
|---|---|---|---|
| decay .7 / ρ 1.0 (probe raw) | 156,974 | 1 | |
| decay .9 / ρ .3 | 121,810 | 1 | damping >> raw |
| decay .95 / ρ .2 | 114,437 | 0 | ~champion |
| decay .9 / ρ .3 + **prox 2** | **112,234** | 0 | **beats champion, single-core** |
| decay .9 / ρ .3 + blockers .5 | 152,325 | 1 | REJECT (poisons price signal) |

→ defaults = decay .9 / ρ .3 / prox 2. Wall single runs: directional only (portfolio
convergence is machine-load sensitive).

## 07-07 — hard-family validation T=60 (ACCORD defaults vs PRISM, serial wall)

Single wall runs (`.claude/scratch/_val_{acc,prism}_*_60.log`), all 12 oracle-feasible
(stage 5). NOTE the asymmetry: ACCORD single-core using **39s** of 60 (static poly
reserve oversized off the tardy a_pref floor) vs PRISM 4-core using ~55s.

| inst | ACCORD v1 | PRISM 4-core | Δ | read |
|---|---|---|---|---|
| T1 | 31,156 (Z1=1) | 6,533 | +377% | the known deterministic trap: per-block prices name the VICTIM, but the fix is an in-bay rearrangement (blocker-shaped signal); 4,456 iters spin |
| T11 | 40,691 | 32,067 | +26.9% | Z3-refinement-bound |
| T13 | **112,234** | 114,810 | **−2.2%** | WIN (single-core!) |
| T17 | 63,162 | 57,581 | +9.7% | Z3-refinement-bound |
| T18 | **72,408** | 75,140 | **−3.6%** | WIN — historically PRISM's problem instance |
| T20 | 297,690 (Z1=4) | 238,855 (Z1=2) | +24.6% | heaviest packing; 84 iters only (pack cost dominates) |

2W/4L on day one vs the tuned champion = strong for a fresh paradigm
([[new-mechanism-long-view]]). Extra config check: decay .95/ρ .2 + prox2 on T13 =
115,746 > 112,234 → defaults confirmed (decay .9 / ρ .3 / prox 2).

**Next fix applied: dynamic build reserve** — re-size the poly reserve off the CURRENT
best's Z1 each iteration (best is monotonic → reclaim is regression-free). Reclaims the
~28% idle budget (39s → ~55s of loop). Re-validation: `_val2_acc_*_60.log`.

## 07-07 — dynamic reserve re-validation: budget reclaimed, bests UNCHANGED → the binding
constraint is exploration diversity, not budget

`_val2_acc_*_60.log`: wall 39→54s on Z1=0 instances, iterations +30-50% (T13 203→298,
T17 799→1202, T18 279→404), **every best identical** (T1 31,156 / T11 40,691 / T13
112,234 / T17 63,162 / T18 72,408 / T20 297,690 [Z1>0 → reserve correctly stays large]).
The price-jitter exploration SATURATES its reachable basin set long before the budget.
Keep the fix (harmless, monotonic, and any future exploration lever now has 28% more
room); the next levers, in expected-value order:
1. **restart diversity in price space** (stall → full price re-seed w/ fresh rng, not
   just multiplicative jitter) — the analog of PRISM's div01 lesson;
2. **λ sweeps inside the loop during stalls** (price structure kept, Z2 pressure varied);
3. **portfolio of 4 price loops** (diverse seed/ρ/decay per core — v1 is single-core);
4. **blocker-shaped price signal for the T1-trap family** (v1 uniform blocker charging
   regressed T13; needs a shaped/conditional form — the T1 read says the information
   content is right, the delivery is wrong).

## 07-07 — T=180 spot check: short-budget competitive, long-budget SATURATING

| inst | ACCORD@180 | PRISM@180 | note |
|---|---|---|---|
| T13 | 112,234 (= @60 exactly; 893 iters) | 85,404 | loop explores nothing new past ~300 iters |
| T20 | 187,172 (Z1 4→0, −37% vs @60) | 113,657 | scales, but slower than PRISM |

## 07-07 — price-space restart kick: INSTANCE-SPLIT, env-gated (ACCORD_JITS), default OFF

Motive: jitter saturation above. Kick = after N fruitless jitters, re-seed prices as
random evictions (sample n/20 blocks, price them off their incumbent bay at a 1-5-units-
tardy-equivalent level), re-converge from `best` (monotonic best → regression-free in
the guard sense, but it REDIRECTS the trajectory):

| variant | T13@180 | T17@60 | verdict |
|---|---|---|---|
| v1 no kick (default) | **112,234** | 63,162 | |
| pure-reset kick (JITS=3) | 139,580 (+24%) | **59,096 (−6.4%, → +2.6% of champion)** | instance-split |
| stability-center kick (restart from best-known price map + evictions) | 133,331 | 63,295 | loses BOTH → removed |

Same pattern as [[guided-destroy-portfolio]]/div01: exploration kicks win exploration-
hungry instances (T17) and destroy accumulation-driven ones (T13, where the learned
congestion map is the asset). → shipped as `ACCORD_JITS` (default 0 = v1 behavior;
portfolio-diversity candidate). Exception kept unconditionally: kick fires when prices
are EMPTY at a stall (Z1=0 idle spin — v1 did nothing there, any exploration > none).

Caveat: all single wall runs (n=1); magnitudes carry noise, the instance-split DIRECTION
is consistent across variants.

**Final default verification (07-07, `_val5_*`):** defaults (JITS=0) reproduce the v1
optima exactly — T13@180 = 112,234, T17@60 = 63,162; `ACCORD_JITS=3` reproduces the T17
kick win (59,096). Shipped on branch `accord-solver`.
