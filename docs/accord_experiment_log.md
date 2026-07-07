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

## 07-07 — v2 = saturation refine-tail (ACCORD_REFINE, default ON): 5/6 improved, 0
regressions, single-core ACCORD now 3W/2T/1L vs the 4-core champion

Design origin: the trajectory data above, quantified — of each run's iterations, the
share AFTER the last new best is 54-100% (T1 100% [last best at it 4 of 4,629!], T17
92%, T13@180 84%, T18 65%, T11 64%, T20 68% — but T20's max gap between successive
bests is only 15 with 84 total iters, i.e. NOT saturated, just short). Largest observed
gap between successive bests anywhere: 85 (T17). → **patience = 120 iterations without
a new best ⇒ the loop is saturated**; hand the remaining search window to guarded
refinement of the incumbent: `_refine_tail` = ILS over {K._climb_lahc(L=1) →
K._z3_refine (guided swap) → K._ejection_refine (chains)} with monotonic accept,
starting FROM best. Two-layer guard: refiner accepts only K.total_obj improvements AND
the result is re-scored on the oracle before replacing best. The movers reach exactly
the states the assignment MIP cannot express (in-bay/chain rearrangements = the T1
trap; Z1=0-preserving Z3 exchanges = the T11/T17 refinement-bound losses). Dynamic
build reserve re-read each round via o1_holder (a Z1 repair inside the tail reclaims
the tardy reserve on the spot — T1 does exactly this). `ACCORD_REFINE=0` = exact v1
(verified: T17@60 63,162 reproduced); skipped under `ACCORD_ITERS` (tail is
wall-driven).

Results (single wall runs `.claude/scratch/_v2_family_60.log`, all oracle-feasible
stage 5, wall ≤54.5s @60 / 162.5s @180 — no overrun):

| inst | v1 | v2 | Δ | refine gain/secs | vs PRISM 4-core |
|---|---|---|---|---|---|
| T1@60 | 31,156 (Z1=1) | **6,533 (Z1=0)** | **−79.0%** | 24,623 / 52.9s | 6,533 → **0.0% TIE** (exact value match) |
| T11@60 | 40,691 | **30,464** | **−25.1%** | 10,227 / 14.8s | 32,067 → **−5.0% WIN** |
| T13@60 | 112,234 | **111,130** | −1.0% | 1,104 / 6.4s | 114,810 → **−3.2% WIN** |
| T17@60 | 63,162 | **57,621** | **−8.8%** | 5,541 / 40.6s | 57,581 → +0.07% ~tie |
| T18@60 | 72,408 | **55,611** | **−23.2%** | 16,797 / 19.1s | 75,140 → **−26.0% WIN** |
| T20@60 | 297,690 | 297,690 | 0% | not fired (saturated=False) | 238,855 → +24.6% LOSS |
| T13@180 | 112,234 | **86,593** | **−22.8%** | 25,641 / 112.6s | 85,404 → +1.4% |

Reads:
- **T1 trap SOLVED** — the LAHC/chain movers repair the Z1=1 in-bay arrangement the
  per-block price signal could only point at; lands on PRISM's exact champion value.
  The planned "blocker-shaped price signal" lever is now largely moot.
- **Long-budget saturation broken**: T13@180 −22.8% (was bit-identical to @60); the
  tail scales with budget (112.6s refine → 25,641).
- **T20 untouched as designed**: patience never fires while the loop still finds bests
  (its 84 iters < 120); v1 output reproduced exactly = the guard's no-regression
  property demonstrated live. T20 remains the one loss (pack-cost-dominated; the loop
  is budget-bound there, not diversity-bound — 4-core is the lever for it).
- Refine-tail beats the kick everywhere the kick helped (T17 57,621 < kick's 59,096)
  without the kick's T13 damage → `ACCORD_JITS` stays 0; the portfolio-diversity role
  the kick was reserved for is partly absorbed by the tail.

Caveat: single wall runs (n=1) vs v1 numbers measured earlier the same day; but the
loop is seeded/single-threaded (T20 & the ACCORD_REFINE=0 control reproduce v1
EXACTLY), so the deltas are real, not machine-load noise.

Next levers, re-ranked after v2: ① 4-core price-loop portfolio (T20-class needs
throughput; diverse ρ/decay/patience per worker — license: loop MIPs are Gurobi,
single-use ⇒ needs serialized MIP access or in-process interleave) ② in-loop λ sweeps
at stalls ③ full-20 sweep + grader zip once T20-class is addressed.

## 07-08 — v3 = measured build reserve for tardy incumbents: T20 −32.1% (Z1 4→0),
the LAST champion loss flips → hard family 4W/2T/0L single-core

Found while wall-checking the heavy instances: T20@60 total wall was 38.4s and
T38@60 46.5s — for incumbents that STAY tardy the static 30% poly reserve never
shrinks, yet the REAL build off warm caches costs <1s (T20 measured 0.2s, T14 0.13s,
T1 0.02s). ~22s of a 60s budget sat idle on exactly the pack-heavy instances that are
budget-bound. Fix: at the would-be time-break (and before refine entry) with Z1>0,
run ONE real `_score_and_pack` and re-size the reserve to min(static, 2.5×measured)
— the bridge idle-ILS multiplier, self-adjusting. Gate: measure only when
6×iter_cost < static reserve (poly escalation ≲ ~15× a mask eval ⇒ T38-class skips,
keeps the full reserve, pays nothing).

| inst @60 | before | after | Δ | note |
|---|---|---|---|---|
| T20 | 297,690 (Z1=4, 84 it) | **202,262 (Z1=0, 115 it)** | **−32.1%** | beats PRISM 4-core 238,855 by **−15.3%** — last loss flipped; near v1@180's 187k at 1/3 budget |
| T14 | 104,044 (113 it) | **97,491 (186 it)** | **−6.3%** | |
| T38 | 87,487,961 | = (build_cost None) | 0% | gate skipped; wall 46.0s no overrun |
| T13 | 111,130 | 112,234 | +1.0% | NOT the lever (Z1=0 path untouched): machine load ate the marginal 6.4s refine window this run → v1 value; boundary noise |
| T1 | 6,533 | **6,533** | 0% | refine-entry measurement path verified (build 0.02s, refine 52.4s) |

Walls 46.0–55.1s @60, all feasible stage 5, no overrun. **Hard family @60,
single-core ACCORD vs the 4-core PRISM+MO+SWAP champion: T11 −5.0%, T13 −2.2%,
T18 −26.0%, T20 −15.3% wins; T1, T17 ties; zero losses.** T20's refine still never
fires (saturated=False — still budget-bound, improving at cutoff) → the 4-core
portfolio remains the next multiplier for it.

## 07-08 — full-20 @60 sweep: 20/20 feasible, walls 54.3–55.3s, refine fires 18/20

`.claude/scratch/_v3_sweep.log`. 19/20 reach Z1=0 (T14 Z1=1); refine-tail gains are
broad (T12 −35k, T1 −24.6k, T16 −23.8k, T10 −20.8k, T18 −16.8k, T7 −16.0k, ...).
Reproducibility: T1/T11/T13/T17/T18/T20 all land on previously measured values; T13
gets its refine window back (111,130) confirming the +1% run above was load noise.
**T20@180 = 117,129 (−37.4% vs v1@180 187,172; PRISM@180 113,657 → +3.1%)** — the
@60 gap of +65% is nearly closed at 180s; refine reclaimed 85k over 66.5s.

## 07-08 — v4 = bay-parallel pack pool (ACCORD_PAR, default ON): eval wall −26..−57%
freed into iterations/refine — T13 −3.1%, T14 −3.2%, T20@180 −1.0%, zero losses

Why this and not a trajectory-diverse 4-core portfolio: T20-class needs THROUGHPUT
on one accumulating price map (diversity splits it), pack results are deterministic
per (bay, ids) so parallel packing leaves the trajectory EXACTLY the single-core one
(verified: fixed-30-iter best bit-identical PAR on/off; production T20@180 same
211 iters / last_best 90), and packing never touches Gurobi → no single-use-license
contention (the MIP stays serial in the master). `accord/pack_pool.py`: spawn-probe
+ serial fallback ([[portfolio-spawn-guard]]), 4 workers, oracle farms cache-miss
bays via map_async (timeout-guarded; any failure degrades to serial permanently).

Phase decomposition (T20, fixed 30 it): MIP 3.6s (12%) / eval 9.3s (63%) → pool cuts
eval to 6.9s @30it (bay-level Amdahl: the biggest bay is the critical path) and to
−57% at @180 (worker caches warm). The reclaimed wall flows to whichever phase is
binding: more loop iterations when budget-bound (T20@60 124→201 it) or a longer
refine tail when saturated (T20@180 refine 72→106s; T13@60 refine 6.7→19.9s).

**Cold-cache build incident (fixed):** with workers packing every bay, the MASTER's
packing caches stay cold, so even a Z1=0 final build costs ~5-12× the warm one
(T14 0.14→0.71s, T20 →2.47s) — the bare 4% Z1=0 reserve degraded T20@60's final
build to AABB: true obj 468,932 (Z1=10) while the internal best was 202,262 (Z1=0).
Fix: extend the v3 measured-build mechanism to fire whenever the pool is active
(not just Z1>0); the measurement itself warms the master's caches, so the final
build then runs at warm cost. Re-run: T20@60 true obj = best exactly (202,262).

| inst | serial | ACCORD_PAR=1 | Δ |
|---|---|---|---|
| T13@60 | 111,130 | **107,689** | **−3.1%** |
| T14@60 | 97,491 | **94,386** | **−3.2%** |
| T20@180 | 116,504 | **115,388** | −1.0% |
| T20@60 | 202,262 | 202,262 | 0 (loop saturates at it 90 regardless) |
| T1@60 | 6,533 | 6,533 | 0 (spawn ~2s off the refine tail, harmless) |
| T38@60 | 87,487,961 | = | 0 (iters 2→3; wall 49.2s, timeout-robust) |
| T17@60 | 57,621 | 57,621 | 0 (entry-default smoke) |

No losses, no overruns (max wall 56.7s @60). **Default ON** in accord/myalgorithm.py
(`ACCORD_PAR=0` = serial). Upgrade path if more is wanted: (bay × order) job grain
under MULTIORDER breaks the biggest-bay critical path (~3× eval ceiling vs the
current ~2×); the refine tail's total_obj is still serial (master-side).
