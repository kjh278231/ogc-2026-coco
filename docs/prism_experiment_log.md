# PRISM Experiment Log (third algorithm, v2)

Separate from BRIDGE (`docs/experiment_log.md`) and the older covering prototype
(`docs/third_algorithm_experiment_log.md`). This file tracks **PRISM**: a third solver
built around a *spectrum of preference-ideal MIP anchors*, each refined by memetic LAHC,
combined by best-of + guarded recombination.

Goal (user, 2026-06-29): a genuinely different third algorithm, considering the problem
type and the P1–P6 characteristics, then improve it continuously. It is expected to start
behind the heavily-tuned BRIDGE.

Entry point: `prism/myalgorithm.py` -> `prism/prism_engine.py:prism_solve`. It reuses ONLY
the validated packing/scoring/LAHC/recombine kernel from `bridge/solver.py` (imported as
`K`); the anchor-spectrum orchestration is new. Mirrors the covering prototype's
"reuse only the kernel" rule. Probes live in `tools/_facet_*.py`, A/B in `tools/_prism_ab.py`.

---

## Problem structure (recap, code-verified)

`total = w1·Σ_bay T_bay(A) + w2·Z2(A) + w3·Z3(A)`, near-lexicographic (w1 ≫ w3 ≫ w2).
- **Z3** (preference) and **Z2** (workload max-imbalance) are instant functions of the
  assignment `A` alone — no packing.
- **Z1** (tardiness) is a per-bay packing sum, ≈0 on 17/20 train instances, and is
  **packing-driven** (shape tiling), with **no cheap surrogate** (Exp 0/2: ρ=0.19).
- So most instances reduce to: `min w3·Z3 + w2·Z2` s.t. each bay packs on-time; Z1 binds
  only on the hard-packing instances (T20/13/14/11/18 ≈ the P4–P6 family, BRIDGE's weak
  spot per [[grader-best-0619-2]]).

## Diagnosis chain — three cheap falsifications (eval-count fixed)

| # | Idea | Probe | Verdict | Evidence |
|---|------|-------|---------|----------|
| 1 | **Global MIP-Benders**: master `min w2Z2+w3Z3` + packing-feasibility cuts | `_benders_probe.py` (no-good cut), `_facet_probe.py` (superset soft-penalty) | **REJECTED** | MIP finds Z3 5–30× lower than ILS but Z1 explodes; neither cut drives global Z1→0 (crowding patterns are exponential, whack-a-mole). T5 Z3 138 vs ILS 658 but Z1=53 → +700% obj |
| 2 | **MIP-LNS / fix-and-optimize**: free K bays, re-assign optimally with local packing-Benders | `_facet_lns_probe.py` | **REJECTED** | MIP-per-move too slow (~150 moves/30s on T20); never catches cheap-move search. Consistent with the team using recombine once-at-end, not continuously |
| 3 | **Anchor-seeded LAHC**: seed the validated LAHC descent from the MIP ideal a* | `_facet_anchor_probe.py` | **SURVIVES (complementary)** | a*-seed vs a_pref-seed LAHC: **T20 −6.0%, T13 −11.6%, T15 −21.9%, T17 −20.1%** wins; T5 +15%, T10 +22%, T11 +61%, T14 +21% losses. Both reach Z1=0. a* also slashes Z2 (T20 1372 vs 3522) |

**Surviving asset:** the global assignment MIP's Z3/Z2 power is real; the only failure is
controlling Z1. The cheap fix is to use the MIP solution as a *seed* for the validated
cheap-move LAHC, which repairs Z1→0 while keeping the low Z3/Z2 — a strong complementary
member (instance-split like [[guided-destroy-portfolio]], [[alns-seed-recombine-instance-split]]).

## PRISM design

Do not commit to one anchor. Sweep a **spectrum** of anchors spanning the
preference↔balance trade-off, refine each into its own Z1=0 basin with LAHC, emit best-of
(+ recombine over every bay-piece the spectrum generated):

```
anchors = { a_pref, a_balanced_load, a_pref_capped }            # validated heuristic seeds
        ∪ { argmin(lam·w2·Z2 + w3·Z3) : lam ∈ PRISM_LAMBDAS }    # MIP preference ideals
for each anchor a:  A_a = LAHC(a, shared cache)                 # repair Z1→0, keep low Z3/Z2
best = best-of_a A_a ; best = recombine(pool, best)             # guarded, Pareto-safe
materialize best with polygon-escalation build
```

`PRISM_LAMBDAS` default `1,8,64` (lam=1 = the true-weight ideal a*; larger lam spreads
load → less crowding → easier packing → a different Z1=0 basin). The diversity is the
mechanism: best-of over the spectrum captures whichever basin wins per instance.

Distinct from: BRIDGE (single a_pref-anchored ILS/LAHC + mip_repair as one candidate),
ALNS (destroy/repair LNS), covering (set-partition over a perturbation pool). PRISM
harnesses the MIP-Z3 asset the falsified approaches could not.

---

## Results

### Determinism fix (mandatory for eval-count A/B)

PRISM was non-deterministic across processes from TWO Gurobi sources, both now fixed/handled:
- **MIP anchor**: parallel MIP (Threads>1) is non-reproducible even with a fixed seed (ties
  broken by thread race); also `lam=64` took ~3.4s and grazed the 4s TimeLimit → sometimes
  no incumbent. Fix: `Threads=1, Seed=0` (the lam=1 anchor solves in ~0.02s), default
  `PRISM_LAMBDAS=1` (drop the slow high-lam), optional deterministic `WorkLimit`.
- **recombine**: `_recombine` uses `SOLVER_CP_WORKERS` threads + a wall TimeLimit → parallel
  non-determinism. A/B harness pins `SOLVER_CP_WORKERS=1` for both algos (fair; matches the
  portfolio workers). With these, PRISM is bit-reproducible (T17 91,450 ×N).

### v0 (even-split) — PRISM vs BRIDGE, E=4000, `_score_and_pack` basis  ⚠ HANDICAPPED

Serial PRISM splits the E-eval budget evenly across its 4 anchors (~1000 evals each) while
BRIDGE spends all 4000 on one pipeline. Deterministic result (`_prism_ab_evensplit_partial.txt`,
T1–T11):

| inst | BRIDGE | PRISM | Δ% | note |
|------|-------:|------:|---:|------|
| T1  | 165,754 | 151,104 | **−8.8** | PRISM win (Z2 33 vs 1009) |
| T9  | 143,650 |  99,535 | **−30.7** | PRISM win |
| T2,T8 | tie | tie | 0 | trivial |
| T3  |  74,920 |  87,430 | +16.7 | loss |
| T4  |  86,549 | 177,719 | **+105** | loss — PRISM Z1=5 (a* over-crowds, 1000 evals can't repair) |
| T5  |  83,868 | 122,589 | +46 | loss |
| T6  | 145,958 | 167,970 | +15 | loss |
| T7  |  89,311 | 151,794 | +70 | loss — PRISM Z1=2 |
| T10 |  87,752 |  92,708 | +6 | loss |

**Diagnosis: the even split is the problem, not the anchors.** Each anchor (esp. a*, which
over-crowds and needs evals to repair Z1) is starved at ~1000 evals → emits high-Z1 on the
packing instances (T4/T7). An earlier *non-deterministic* run looked far better only because
it caught lucky parallel-recombine outcomes — discard it. **Conclusion: PRISM's value
requires per-anchor full budget (parallelism); even-split serial is worse than BRIDGE.**
The fix is the parallel portfolio (one anchor per core, each full budget) — `prism/portfolio.py`,
gated T≥180 — which is also the natural deployment.

### v0 (full-budget) — each anchor gets E=4000 (parallel deployment proxy)  ✅ PROMISING

`tools/_prism_ab_fb4000.txt`, `PRISM_ANCHOR_FULL_BUDGET=1`, deterministic. Full-20:

**PRISM 10 wins / BRIDGE 7 / 3 ties; aggregate −0.1% (dead even); oracle best-of −9.3%.**

| group | instances | read |
|-------|-----------|------|
| PRISM wins | T1 −8.8, T9 −30.7, T12 −20.2, T13 −11.6, T14 −15.3, T15 −21.9, T17 −11.1, T18 −0.4, T19 −12.9, T20 −7.0 | **the hard/large + Z3-heavy family — exactly BRIDGE's weak P3–P6 zone**; MIP anchor's Z2 power shows (T13 Z2 1411 vs 3685, T14 592 vs 1149) |
| BRIDGE wins | T3 +17, T4 +105, T5 +46, T6 +15, T7 +70, T10 +6, T16 +7 | smaller/packing-sensitive; PRISM emits Z1>0 (T4 Z1=5, T7 Z1=2) — see below |
| ties | T2, T8, T11 | trivial / converged |

This is far stronger than the covering prototype standalone (+5..+13%): PRISM is **already
~even with BRIDGE on aggregate and wins the hard family**, with real −9.3% oracle complement.
NB favorable to PRISM (16000 vs 4000 work); the fair test is portfolio-vs-portfolio (below).

### Rejected: per-anchor Z1-repair-first (`PRISM_REPAIR_FIRST`)

Hypothesis for the BRIDGE-win losses: PRISM's pure LAHC under-repairs Z1 from over-crowding
anchors (a_pref maximises preference → over-crowds preferred bays → Z1>0; the MIP anchors
likewise). Tried `local_search` (BRIDGE's tardy-bay first-improvement) BEFORE LAHC per anchor.
**Rejected — net negative**: T5 −3.6% but T6 +111% (Z1 0→7), T7 +39%, T4 +5%. Cause:
local_search shares the per-anchor budget and on a high-Z1 start does not converge within it,
burning evals it leaves for nothing (and is a no-op on the already-low-Z1 a_pref anchor). The
v0 default stays pure LAHC. **Read: the BRIDGE-win instances are where BRIDGE's full pipeline
(improved_search + mip_repair + recombine) is simply stronger; PRISM is a complementary
specialist for the hard/large family, not a universal replacement.** A budget-capped or
MIP-based local Z1-repair may still help — deferred.

### Portfolio (deployment): one anchor per core — HONEST VERDICT: loses standalone

`prism/portfolio.py` (gated T≥180), `tools/_prism_portf_ab.py` (wall, real `algorithm()`
entry, scored by `utils.check_feasibility`). Smoke OK (T13@180 feasible, wall 160.9s).

**PRISM-portfolio vs BRIDGE-portfolio, wall T=180, true grader objective**
(`tools/_prism_portf_ab180.txt`, alternated so wall noise is controlled):

| inst | BRIDGE-pf | PRISM-pf | Δ% | |
|------|----------:|---------:|---:|--|
| T13 | 175,818 | 159,779 | **−9.1** | PRISM |
| T17 |  75,925 |  66,273 | **−12.7** | PRISM |
| T5  |  66,631 |  65,801 | −1.2 | PRISM (tiny) |
| T15 |  50,382 |  50,382 | 0 | tie |
| T7  |  74,986 |  79,626 | +6.2 | bridge |
| T1  |  41,024 |  48,841 | +19 | bridge (the a*/MIP-seed trap, [[alns-seed-recombine-instance-split]]) |
| T14 | 182,829 | 245,154 | +34 | bridge |
| T20 | 266,393 | 505,861 | **+90** | bridge (PRISM Z3 3965 vs 2041) |

**Verdict: PRISM v0 as a standalone portfolio LOSES to the (heavily-tuned) BRIDGE portfolio.**
It wins only T13/T17 (mid, Z3-heavy, packing-easy); it loses badly on the heavy packing
(T20 +90%, T14 +34%) and the trap (T1 +19%). best-of(PRISM-pf, BRIDGE-pf) oracle ≈ **−2.8%**
(from T13/T17/T5) — modest, because the BRIDGE portfolio already captures most of it.

**Correction to the full-budget read.** The full-budget eval comparison (−0.1% / −9.3% oracle)
was OVER-OPTIMISTIC: it compared PRISM (16000 work) to BRIDGE-*single* (4000 work), and the
BRIDGE *portfolio* (div01 trap-escape + union-recombine over 4 diverse pools) is far stronger
than BRIDGE-single — especially on exactly the heavy/trap instances PRISM hoped to win (T20/
T14/T1). So PRISM's hoped-for P4–P6 niche does NOT hold against the real BRIDGE portfolio; its
genuine complementary value is the MID Z3-heavy family (T13/T17).

**⚠ THE ABOVE PORTFOLIO TABLE IS INVALID — import-shadowing bug.** `prism_engine.py` did
`sys.path.insert(0, BRIDGE_DIR)` to reach the kernel, which put bridge FIRST on sys.path, so
prism/myalgorithm.py's `import portfolio` silently resolved to **bridge/portfolio.py** (bridge
also has a portfolio.py). So every "PRISM portfolio" run actually ran BRIDGE's portfolio under
PRISM's crippled env (no LAHC/unified/mip_repair set) — that is the real source of the T20
+90% (a degraded BRIDGE), NOT PRISM. The serial / full-budget eval results above are UNAFFECTED
(they call `prism_engine.prism_solve` directly, which never imports `portfolio`). Fix: append
BRIDGE_DIR instead of insert-at-0 so PRISM's own modules win. Confirmed via `portfolio.LAST`:
post-fix, T20@T=60 runs `mode=portfolio, 4 workers (pref/balanced/capped/mip1), union_recomb`,
final 369,737 (= the capped anchor; the mip1/a* anchor is the WORST worker here at 669,982 —
it over-crowds on heavy packing). Real PRISM-portfolio vs BRIDGE-portfolio A/B re-running
(`tools/_prism_portf_ab180_fixed.txt`).

### Bug 2 — workers terminate early (single LAHC plateau), wasting the budget

With the import bug fixed, the real PRISM portfolio at T=180 LOST on all 8 (`_prism_portf_ab180_fixed.txt`: T1 +268%, T7 +102%, T5 +84%, T13/T20/T14 +30-42%) — **but the PRISM
wall was only 5–52s of the 180s budget.** Cause: `refine_anchor` ran a SINGLE `_climb_lahc`,
which breaks at the first no-improve sweep (plateau) and returns; the worker then idles. BRIDGE
fills the budget with its ILS/idle loops. So PRISM was searching ~1/10–1/30 as long as BRIDGE.

**Fix:** `_refine` now wraps the LAHC descent in an ILS perturbation loop (BRIDGE's `_ils` shape
but LAHC + anchor seed): plateau → `_perturb` (re-home a few blocks) → re-LAHC → keep best,
until the deadline.

### Bug 3 — ILS loop exposed a worker-collection RACE (catastrophic)

With the ILS loop, the first re-run was CATASTROPHIC (T13 1.96M Z1=38, T20 5.1M Z1=144) despite
full wall (168s). Cause: the workers now run to their deadline (= gather_dl), but the master's
gather loop stops AT gather_dl and `pool.terminate()` KILLS the still-running workers before
they return → `results` empty → master falls back to a time-starved serial solve (~30s left)
→ garbage. (Pre-ILS the workers plateaued early and returned in time, hiding this.) BRIDGE's
workers reserve internal safety so they return early; PRISM's `_refine` runs to the deadline.
**Fix:** `portfolio.py` shaves a `collect_margin = max(3, 0.05·T)` off `worker_tl` so workers
finish and return BEFORE the master stops gathering.

**Verification (T13@T=180, post-fix, `portfolio.LAST`):** `mode=portfolio, 4 workers collected
(no err), union_recomb (pool 85507)`, obj **176,694** [Z1=0, Z2=1344, Z3=1278], wall 160.4s.
vs BRIDGE-portfolio 175,818 → **≈ tied (+0.5%)**. The ILS loop helped a lot (176,694 vs the
pre-ILS 249,883). NB the *pref* anchor won here (176,694); the mip1/a* anchor gave 204,818 —
at full ILS budget the anchors converge closer, so the MIP anchor's edge narrows. Full
8-instance corrected A/B running (`_prism_portf_ab180_v2.txt`).

### Breakthrough — per-worker restart diversity (seed + mover-shuffle)

Corrected portfolio (all 3 bugs fixed) was 3W/5L (+7.6% agg), dominated by **T1 +167%**: every
worker used the SAME rng (default seed), so the 4 anchors funnelled into the SAME basin and none
escaped T1's deterministic Z1=1 trap. **Fix:** give each worker a distinct seed (`20260629 +
1000·i`) that drives BOTH the ILS kick AND the `_climb_lahc` per-sweep mover shuffle (restart
diversity = BRIDGE's div01 mechanism). Result (`_prism_portf_div.txt`, T=180, all Z1=0):

| inst | BRIDGE-pf | PRISM-pf (diverse) | Δ% |
|------|----------:|-------------------:|---:|
| T1  | 41,024 | 32,188 | **−21.5** (trap escaped + beats bridge) |
| T20 | 266,393 | 223,903 | **−16.0** (was +2.3!) |
| T17 | 75,925 | 69,389 | **−8.6** |
| T13 | 175,818 | 168,579 | **−4.1** |

A decisive lever: restart diversity over the anchor workers turns PRISM from "≈ tied / loses T1"
into **beating BRIDGE-portfolio on the hard/Z3 family**. Full 8-instance re-confirm in progress
(`_prism_portf_ab180_v3.txt`).

### Full-20 portfolio A/B — PRISM beats BRIDGE −6.9%

Wall T=180, true grader obj. PRISM-portfolio (seed-diverse) vs BRIDGE-portfolio. The 12 non-hard
instances were run PAIRED (prism then bridge back-to-back → drift-controlled); the hard-8 PRISM
are from v3, hard-8 BRIDGE from the earlier valid run (paired re-run in progress to remove the
drift caveat).

| | result |
|---|---|
| **PRISM 13W / BRIDGE 5W / 2T** | **aggregate −6.9%** (1,587,776 vs 1,705,362), oracle −8.0% |
| big wins | T11 −24.9, T1 −21.5, T4 −17.6, T20 −16.0, T6 −10.6, T15 −10.2, T17 −8.6, T9 −8.3, T7 −7.7, T14 −5.5, T10 −5.1, T13 −4.1, T19 −2.3 |
| small losses | T3 +2.4, T5 +2.9, T12 +1.9, T16 +1.8 (all ≤3%) |
| one real loss | **T18 +19.4** (PRISM Z3 510 vs 429) — investigate |
| ties | T2, T8 (trivial) |

**Verdict: PRISM-portfolio EXCEEDS BRIDGE-portfolio on train full-20 (−6.9%), wins large/loses
small.** The paired hard-8 re-run returned **bit-identical** objectives to the earlier runs
(T1 32188, T20 223903, … all exact) → the T=180 portfolio is effectively **deterministic**
(search fully converges in budget; recombine deterministic at CP_WORKERS=1). So −9.0% (hard-8) /
−6.9% (full-20) are NOT wall-noise or drift artifacts, and A/B need not be repeated. Remaining
caveat: **train T* ≠ grader P1–P6 — validate by submitting `myalgorithm0629-prism.zip`**
([[anchor-to-grader-best]], [[grader-p1-p6-distinct]]).

### Submission zip — VALIDATED
`myalgorithm0629-prism.zip` (51.7 KB): flat `myalgorithm.py + prism_engine.py + portfolio.py +
solver.py + packing.py + utils.py`. End-to-end smoke from the EXTRACTED zip (`tools/_prism_zip_smoke.py`,
T20@T=180): **feasible (stage 5), obj 223,903 (= in-repo PRISM-portfolio exactly → flat packaging
preserves behaviour), wall 157.5s (no overrun), spawn OK.** Ready to submit for grader validation.

### T18 (+19.4%, the one real loss) — complementary-coverage gap, not a bug

`portfolio.LAST`: workers [pref 101356, **balanced 81702**, capped 98264, mip1 104656], union-
recombine over 122085 cols did NOT improve → final 81702. BRIDGE 68433 [0,2844,429] vs PRISM
81702 [0,3468,510] (worse Z2 AND Z3). **No PRISM anchor/recombine reaches T18's good basin;
BRIDGE's div01/L-diverse workers do.** A genuine search-coverage gap, not a defect — exactly
the case where the oracle best-of(PRISM,BRIDGE) takes T18 from BRIDGE. PRISM wins 13/20, so not
worth chasing one loss at the 4-core cap's expense; it argues for a COMBINED portfolio (some
BRIDGE-style + some PRISM-anchor workers) as the ultimate deployment.

### P★b (checklist §2) — ADOPTED: portfolio beats serial at EVERY budget → gate 180→45

The anchor-portfolio gives each of 4 workers the FULL budget on its own core; the single-process
path even-splits across 4 anchors (~1/4 each) and starves them. Portfolio vs serial PRISM:

| T | T13 | T20 | T17 |
|---|-----|-----|-----|
| 120 | −6.5% | **−43.6%** (serial Z1=7) | −5.8% |
| 60  | −15.0% | **−59.1%** (serial Z1=11) | −21.6% |

So the old "T=60 portfolio regressed" verdict was the budget-SPLIT V1 and does NOT apply to the
anchor-portfolio. **Adopted: `PRISM_PORTFOLIO_MIN_T` 180→45** so P1(≤60)/P2 also use 4 cores
(directly fixes checklist §2 — single-process wasted 3 cores). Floor 45: T=40 stays
feasible/no-overrun (wall 28.6s) but heavy-packing quality dips, and the win is only *proven* at
T≥60, so 45 is the conservative capture of P1≤60. Side-finding: the serial path ALSO over-reserves
(`poly_build_reserve`) → ~50s idle at T=120 (checklist §1 in the serial path too). Zip rebuilt
(52.1 KB, gate=45); T=60 extract-smoke confirms portfolio routing + feasible + no overrun.

### PRISM vs BRIDGE at T=60 (short/P1 budget) — PRISM −14.8%

PRISM-portfolio (gate=45 → parallel) vs BRIDGE (gate=180 → single-process) at T=60:
T20 −31.5%, T17 −14.6%, T13 −0.5%, **T11 +43.1% (loss)** → PRISM 3W/1L, **aggregate −14.8%**.
So PRISM beats BRIDGE at BOTH budgets: **T=60 −14.8%, T=180 −6.9%.** Complementary losses:
T11@60 (BRIDGE's single tuned trajectory wins) + T18@180 (coverage gap) — one each, both
where the oracle takes BRIDGE.

**Summary: PRISM is a validated third algorithm that EXCEEDS BRIDGE across budgets on train,**
deployable (zip gate=45, smoke OK T=60/T=180, feasible/no-overrun/reproducible). The decisive
remaining unknown is train→grader transfer → **submit `myalgorithm0629-prism.zip`** (baseline =
grader best BRIDGE 0619-2, [[anchor-to-grader-best]]).

### Checklist §1 — ADOPTED: idle reclaim (margin 12–20s → 4–6s), overrun-safe

Two changes in `portfolio.py`, after grader confirmed the pre-checklist build is "not bad":
1. **Master idle-reclaim ILS** (`PRISM_PORTF_IDLE_RECLAIM`, default on): after best-of+recombine,
   the master returned ~13–23s early (T=180) / ~7s early (T=60). Now it spends the leftover on a
   GUARDED ILS from the best assignment (`refine_anchor` until `poly_dl − emit_margin`), adopting
   only if the true best-of score improves → **monotonic, cannot regress**. The final A2 score
   uses `poly_deadline=poly_dl` so it degrades to AABB rather than overrun.
2. **Right-sized `safety`** `max(2,0.04·T)` → `min(5, max(3, 0.025·T))`: the old buffer ballooned
   to 7.2s @180 (most of the margin); the final build degrades at poly_dl so emit only needs ~3s.

**Verified (heaviest instances, worst-case build):** T20@180 wall 173.9s (margin 6.1), T14@180
174.5s (5.5), T17@60 55.9s (4.1) — **no overrun, obj bit-identical to baseline (monotonic)**.
Idle 12–20s → 4–6s margin. Quality unchanged on these (near-converged; 1-core idle-fill can't beat
the 4-core portfolio) but it's free + monotonic + helps any non-converged (larger grader) instance.
Zip rebuilt with §1; extract-smoke re-confirms overrun-safe.

### Grader validation (P1–P6, 2026-06-30) — train→grader transfer CONFIRMED ✅

`myalgorithm0629-prism.zip` submitted to the real grader. Compared against the historical best
(BRIDGE 0619-2) and the 0629 single-LAHC submission ([[grader-best-0619-2]]):

| P  | 0619-2 (best) | 0629 LAHC  | **PRISM**     | Δ PRISM vs best | Δ PRISM vs LAHC |
|----|--------------:|-----------:|--------------:|----------------:|----------------:|
| P1 | 11,280        | 11,280     | 11,280        | tie             | tie             |
| P2 | 50,056        | 50,056     | **47,376**    | **−5.4% WIN**   | −5.4% WIN       |
| P3 | 494,755       | 418,655    | **391,525**   | **−20.9% WIN**  | −6.5% WIN       |
| P4 | 7,989,521     | 9,195,737  | 8,674,414     | +8.6% LOSS      | −5.7% WIN       |
| P5 | 22,314,093    | 23,537,503 | 22,538,969    | +1.0% (tiny)    | −4.2% WIN       |
| P6 | 47,455,786    | 48,276,312 | **46,523,170**| **−2.0% WIN**   | −3.6% WIN       |

**Verdict: PRISM beats the historical best on P2/P3/P6, ties P1; the only real loss is P4
(+8.6%), P5 is a negligible +1.0%.** Decisively, PRISM **beats single-LAHC on every non-trivial
instance (P2–P6, −3.6..−6.5%)** — exactly the big long-budget P4/P5/P6 where LAHC regressed,
PRISM recovers, and on P6 it even beats the 0619-2 lineage. **The train-set prediction (PRISM
wins the hard/large/Z3 family = the P3–P6 zone, −6.9% full-20) HELD on the real grader.** PRISM
is the strongest single submission to date and the first to beat 0619-2 on multiple instances.

Best-of across all submissions (rank anchor, [[leaderboard-rank-based]]): P1 11,280 / P2 47,376
(PRISM) / P3 391,525 (PRISM) / P4 7,989,521 (0619-2) / P5 22,314,093 (0619-2) / P6 46,523,170
(PRISM). PRISM's only grader weak spot is the >=300s heaviest-packing P4 (and tiny P5) — the
T20/T14 family where the MIP anchor over-crowds → a combined BRIDGE+PRISM portfolio (some
greedy+recombine workers alongside the anchor workers) is the path to >= best everywhere.

### Next (continuous improvement, lower priority until grader feedback)
- **P4 (+8.6%) / P5 (tiny) recovery** — the only grader losses; >=300s heavy-packing where the MIP
  anchor over-crowds. Combined BRIDGE+PRISM portfolio (mix greedy+recombine + anchor workers).
- T11@60 / T18@180 coverage (λ-spectrum or a div01-style anchor); combined BRIDGE+PRISM portfolio.
- Checklist §1 deeper: give WORKERS (4-core) more time (trim final_guard/collect_margin) for
  non-converged instances — higher upside than 1-core idle-fill but overrun-riskier; gate carefully.
- Re-baseline the serial full-budget eval A/B (stale since the ILS loop + mover-shuffle).

### λ-spectrum anchors (2026-06-30) — PRISM STANDALONE improvement (goal: lift the ~50th rank, NO best-of-merge)

Goal: improve PRISM by itself (user: no best-of with BRIDGE). Diagnosis via cheap-falsification
(`.claude/scratch/prism_{lb_gap,anchor_pack,lambda_pack,mip_timing}.py`):

1. **My "MIP anchor over-crowds → huge Z1" hypothesis was FALSIFIED.** Raw-packed (no refinement)
   the lam=1 MIP anchor has *lower* Z1 than a_pref on the hard family (T20 887 vs 1818, T17 178 vs
   535). It is a GOOD start, not a bad one.
2. **The real finding: lam=1 (the shipped default) is the WORST MIP anchor.** Raising lam to 4–16
   spreads workload → kills the dominant w1·Z1 crowding cost at a small Z3 rise → raw-packed total
   drops **−50…−79%** (T13 8.45M→3.87M@λ4, T17 1.78M→0.38M@λ8, T20 23.7M→11.9M@λ4). Best lam is
   instance-dependent (T13/15/20=4, T14=16, T17=8) → a SPECTRUM + best-of captures the winner.
3. **2 of PRISM's 4 portfolio anchors were dead weight:** `capped` is byte-identical to `a_pref`
   on 7/10 hard instances (redundant); `balanced` carries a ruinous Z3 (30–150× a_pref) so it
   never wins best-of. They wasted 2 of the 4 worker cores.
4. **Falsified cheaply:** a count-capacity MIP (cap·n/m blocks per bay) — cap1.15/1.3 are byte-
   identical to lam=1, cap1.0 is worse. Load pressure (lam on Z2), not a count cap, is the knob.
5. lam∈{1,4,8,16} all solve to PROVEN optimality in ≤0.5s at mip_tl=4 (Threads=1,Seed=0,
   deterministic) on every train size — the spectrum is a free front-load.

**Change (`prism_engine._anchors`/`_lambdas`, env-rollbackable):** portfolio workers
`{pref,balanced,capped,mip1}` → **`{pref,mip1,mip4,mip16}`** (`PRISM_LAMBDAS` default `1`→`1,4,16`;
`PRISM_HEUR_ANCHORS` default `pref`). Same 4 workers/4 seeds (restart diversity preserved), just
genuinely diverse strong anchors.

**Wall T=180 A/B (deployed portfolio, deterministic, true obj), NEW vs OLD-shipped:**

| inst | OLD | NEW{1,4,16} | Δ% |
|------|----:|------------:|---:|
| T13  | 183,791 | 128,501 | **−30.1** |
| T20  | 255,667 | 244,972 | **−4.2** |
| T1   | 32,188  | 32,188  | 0.0 (trap, no regression) |
| T17  | 72,828  | 74,843  | +2.8 (spectrum missed λ=8) |

**2W/1L/1T, aggregate −11.7%.** The only loss T17 (P3-family) is because its optimum is λ=8, which
{1,4,16} missed. This kicked off a spectrum search (`cfg_A`/`cfg_C`/`cfg_F`/`old_diag`).

**Per-worker diagnosis (the deciding data, `.claude/scratch/old_diag` + `cfg_*` worker tots).**
Each instance is won by a DIFFERENT anchor → there is no dominating 4-set, only the best cover:
- **pref** wins T11, T20; **balanced** wins T38 (heavy m=3 load-spread) & T14; **mip8** wins T9;
  **mip16** wins T13 (−32%) & T17. → the 4 distinct winners are **{pref, balanced, mip8, mip16}**.
- `capped` ≡ `a_pref` byte-for-byte (redundant, never wins) and `mip1` is the WORST worker after
  repair on every probed instance — both dropped. `mip4` wins only T18/T15 (a wash vs mip8's T9).
- **Wall-time noise caveat:** workers run to a WALL deadline, so restart-sensitive instances vary
  run-to-run. T11's pref worker spanned **38,262 … 127,057 across identical configs** → T11 is NOT
  a reliable A/B signal (the "+32% regression" of {4,8,16} was this noise, not the anchors). Stable
  big effects (T13 −32%, T9 −10%) are trustworthy; treat <±5% as noise.

**Interim config F = {pref, balanced, mip8, mip16}** won big (T13 −31.8%, T14 −12.9%, T9 −9.9%,
T18, T17, T38 −0.7%) BUT regressed the packing-sensitive **T4 +15.1%** — because it kept only ONE
a_pref restart-seed (pref). Per-worker diagnosis (`old_diag`): T4/T7 are won in the shipped build by
the **mip1 worker, which is ≡ a_pref on those instances** — i.e. the shipped trio pref/capped/mip1
gives 3 restart-lottery tickets on the a_pref basin, and dropping 2 of them (F) loses T4.

**FINAL — config D = {pref, balanced, capped, mip16}** (`PRISM_LAMBDAS` default `1`→`16`;
`PRISM_HEUR_ANCHORS` stays `pref,balanced,capped`). The MINIMAL change: replace ONLY the shipped
lam=1 MIP worker with lam=16, leave the heuristic trio intact. Deployed-portfolio A/B vs shipped
{pref,balanced,capped,mip1} (true obj):

| inst | shipped | **D** | Δ% | D winner |
|------|--------:|------:|---:|----------|
| T17  | 72,828  | 66,544  | **−8.6** | mip16 |
| T7   | 69,209  | 63,507  | **−8.2** | mip16 |
| T13  | 183,791 | 168,579 | −8.3* | mip16 (*this run; usually −32%, see noise) |
| T14  | 172,684 | (−12.9 pred.) | **−12.9** | balanced (unchanged worker) |
| T18  | 81,702  | (~−3 pred.) | −3 | mip16 |
| T38 (T=300) | 63,884,676 | (−0.7 pred.) | −0.7 | balanced (unchanged) |
| T9 / T20 / T11 | — | ≈ shipped | ±0 | unchanged workers / noise |
| T4   | 72,129  | 74,548  | +3.4 | (one fewer a_pref seed) |
| T15  | 45,266  | (~+5) | +5 | small |

**Adopted D over F:** D keeps 2 a_pref seeds (pref+capped) so the packing-sensitive regression is
**+3.4% (T4) not +15%**, while still capturing the Z3-heavy wins via mip16 (T17/T7/T13/T18) and
PRESERVING the heavy P4/P5 zone via the unchanged `balanced` worker. It only gives up F's T9 win
(no mip8) — a good trade. D is the minimal-disruption, grader-structure-aligned config: pref+capped
(P1/P2 + packing-sensitive lottery), balanced (P4/P5/P6 heavy), mip16 (P3 Z3-heavy −32%). ⚠ The
portfolio has real wall-noise (the identical mip16 worker gave T13 125,371 ×3 and 168,579 ×1 under
different machine load) → trust the DIRECTION, not single-run magnitudes. Zip
`myalgorithm0630-prism-lambda.zip` rebuilt with config D, extract-smoke OK. Next: submit for grader
validation (baseline = the live 0629 PRISM, [[anchor-to-grader-best]]).

---

## TODO / next

- Complete the E=4000 sweep on the 12-instance set, then full-20; tabulate wins/losses and
  the oracle best-of(PRISM, BRIDGE) value (the covering precedent reached −17..−19% oracle).
- Per-anchor contribution ablation (which lam wins where; is the heuristic-seed trio still
  needed once MIP anchors are in?).
- Wall-clock behaviour at the real deployment budgets (T≥180): the MIP anchor spectrum is a
  fixed front-load cost; check it does not starve the LAHC on the eval-starved big instances.
- Diversity knob study: lam-sweep vs an explicit anti-crowding (Σ load_j²) spreading penalty.
- Parallelism: the spectrum maps naturally onto the 4-core portfolio (one anchor per worker).
