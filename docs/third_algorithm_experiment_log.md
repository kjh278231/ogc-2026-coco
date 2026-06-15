# Third Algorithm Experiment Log

This document is intentionally separate from `docs/experiment_log.md`.
The BRIDGE-family solver history stays in that file; this file tracks only the
independent Set Covering + dedup + pool-regeneration algorithm.

## Scope

Goal: build a third algorithm, separated as much as practical from BRIDGE, while
following `docs/methodology.md`.

Prototype entry point:

- `baseline/covering_solver.py`
- Optional dispatch via `OGC_ALGO=cover|covering|third` in `baseline/myalgorithm.py`

The prototype reuses only the validated per-bay packing/building kernel from
`baseline/solver.py`. The assignment engine is independent:

`seed assignments -> bay-set column pool -> Set Covering MIP with in-MIP dedup
variables z[i,j] -> ILS-like perturbations to create new columns -> repeat`

## Modeling Notes

The covering MIP is candidate generation, not a proof of final quality.

- `x_c` selects possibly overlapping bay columns.
- `z_i_j` assigns each block to exactly one final bay.
- `z_i_j` is linked to selected columns by bay membership.
- `Z2` and `Z3` are computed on the deduplicated final assignment.
- `Z1` remains a column proxy, so final true-score validation is mandatory.

## Smoke Test

T=15, wall-clock, lower is better:

| instance | covering prototype | BRIDGE current | read |
|---|---:|---:|---|
| prob_5 | 728,299 | 733,705 | covering slightly better |
| prob_13 | 2,091,132 | 2,166,007 | covering slightly better |
| prob_17 | 1,438,223 | 1,403,872 | covering worse |

Feasibility: 3/3 oracle-feasible.

A deterministic smoke mode (`COVER_MAX_ROUNDS=4`, `COVER_CP_WORKERS=1`) on
prob_16 reproduced exactly twice:

- objective `1,314,197`
- `Z1=10`, `Z2=368`, `Z3=9199`
- stats matched: `rounds=4`, `columns=146`, `covering_improvements=2`,
  `perturb_improvements=8`

Read: the loop is real and sometimes finds assignments BRIDGE does not hit
quickly, but this was not adoption-grade evidence.

## Ablation

Fixed `COVER_MAX_ROUNDS=6`, `COVER_CP_WORKERS=1`, T=60:

| instance | seed only | MIP only | perturb only | full loop |
|---|---:|---:|---:|---:|
| prob_5 | 1,807,512 | 1,807,512 | 1,130,871 | **824,353** |
| prob_13 | 2,144,851 | 2,144,851 | 2,083,286 | **1,749,258** |
| prob_16 | 1,325,307 | 1,325,307 | **1,263,676** | 1,305,742 |
| prob_17 | 1,506,890 | 1,506,890 | **1,451,314** | 1,497,072 |
| prob_20 | 4,712,138 | 4,712,138 | 4,131,965 | **3,097,326** |

Read:

- seed-pool covering alone does nothing;
- the MIP needs a diversified pool;
- perturbation is the pool-diversity engine;
- covering becomes valuable after diversification on hard/tardy cases;
- covering can still pull the search away from better perturb-only incumbents.

## Guard Probes

Final archive source buckets were rejected:

- `COVER_FINAL_PORTFOLIO=source` protected source diversity, but displaced the
  strongest proxy-ranked candidate on prob_20: `2,513,658 -> 2,937,942`.
- Default remains proxy-top final candidate selection with a small candidate cap.

Preventing MIP candidates from updating the incumbent was neutral:

- `COVER_MIP_UPDATES_BEST=0` did not change prob_5/prob_16/prob_17/prob_20.
- The prob_16/prob_17 weakness was not just "MIP clobbers perturb-only best".

Archive-rank diagnostic:

- `COVER_DIAG_ARCHIVE=24` killed the "good candidate exists but proxy rank hides
  it" hypothesis for prob_16/prob_17.
- The best true-scored candidate among proxy top-24 was already proxy rank 1 and
  still worse than perturb-only.
- So the issue was candidate distribution.

## Shadow Perturb Branch

Fix: add an independent shadow perturb branch.

It keeps a perturb-only incumbent with its own RNG and adds those columns to the
same archive/pool, so covering can explore without destroying the perturb-only
search stream. Reusing the main RNG object was a bad attempt because it changed
the main full-loop candidate sequence.

Current default:

- `COVER_SHADOW_PERTURBS_PER_ROUND=auto`
- shadow enabled as 6 perturbs/round when `seed_tardiness_min <= 40`
- `COVER_SHADOW_SEED=20260609`
- high-tardiness packing cases keep shadow off to protect final-build budget

Evidence trail:

| instance | full no shadow | shadow=6 independent | auto shadow |
|---|---:|---:|---:|
| prob_16 | 1,305,742 | **1,236,113** | **1,236,113** |
| prob_17 | 1,497,072 | **1,404,918** | **1,404,918** |
| prob_20 | **2,513,658** | 3,775,334 | **2,513,658** |

The first threshold (`<=25`) was too narrow. prob_6/prob_18 sit just above it
(`seed_tardiness_min` 38/33) and regressed without the exact perturb shadow.

## Final Materialization Fix

Bug: the prototype scored final candidates on the best-of AABB/polygon basis,
then threw away those placements and rebuilt the selected assignment. On prob_20
this produced:

- `final_bestof_proxy=2,513,658`
- emitted objective `3,473,670`

Fix: final scoring now materializes the packed bays it scored and emits that
exact packing. This avoids spending polygon budget once for scoring and again for
output.

## Current Fixed-Round Result

Fixed `COVER_MAX_ROUNDS=6`, `COVER_CP_WORKERS=1`, T=60, adaptive shadow and
MIP-update policies, emitted objective lower is better:

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| full loop vs perturb-only | 17 | 1 | 2 | 20,958,822 vs 23,836,082 (**-12.1%**) |

Largest wins:

- prob_20: `4,131,965 -> 2,513,658`
- prob_1: `381,279 -> 258,300`
- prob_5: `1,032,599 -> 824,353`
- prob_13: `2,028,415 -> 1,749,258`

Remaining loss:

- prob_11: `1,413,239 -> 1,415,196` (+0.14%)

Emission gaps: none. The emitted objective equals the materialized best-of score
on all 20 instances.

## MIP Update Policy

prob_14 was the main remaining regression under the shadow/default policy:

- perturb-only: `2,005,976`
- full proxy-top: `2,190,766`
- source portfolio: `2,095,133`
- exact shadow + source portfolio: `1,893,451`
- `COVER_MIP_UPDATES_BEST=0`: `1,949,329`

Read: source diversity can rescue prob_14, but source portfolio is known to hurt
prob_20. The safer mechanism is to keep covering candidates as anchors/pool
columns while not letting the AABB-column MIP proxy overwrite the incumbent on
hard-tardy instances.

Whole-train no-update was not safe as a global default:

- prob_14 improved `2,190,766 -> 1,949,329`.
- prob_12 regressed `1,346,764 -> 1,452,636`.
- prob_18 regressed `1,424,089 -> 1,440,116`.

Current default is adaptive:

- if `seed_tardiness_min <= 200`, MIP candidates may update the incumbent;
- if `seed_tardiness_min > 200`, MIP candidates are still anchors/pool columns
  but do not directly update the incumbent.

Representative recheck:

| instance | adaptive default | read |
|---|---:|---|
| prob_12 | 1,346,764 | preserved vs update-on |
| prob_13 | 1,749,258 | preserved vs no-update |
| prob_14 | 1,949,329 | fixes the main regression |
| prob_20 | 2,513,658 | preserved vs no-update |

## Current Read

The third algorithm is now a real independent candidate, not just a BRIDGE patch:

- It has its own assignment loop.
- Set Covering + dedup is useful after the pool has diversity.
- The final materialization fix made validation honest.
- The main unresolved weakness is prob_14, where covering still over-fits the
  proxy/final relationship.

## BRIDGE Head-to-Head

T=60, `bridge` vs actual `third_default` before the CP-SAT worker default was
made deterministic (`COVER_CP_WORKERS` implicit 4 for the third MIP):

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| third_default vs BRIDGE | 4 | 15 | 1 | 18,186,740 vs 12,385,100 (**+46.8%**) |
| best of both, oracle choice | - | - | - | 10,967,573 vs 12,385,100 (**-11.4%**) |

third wins only on:

- prob_1: `317,123 -> 255,694`
- prob_4: `404,409 -> 366,405`
- prob_14: `1,585,723 -> 1,578,099`
- prob_20: `4,106,372 -> 2,795,902`

Read:

- The standalone replacement hypothesis is weak.
- The portfolio/specialist hypothesis is alive: third opens useful basins on a
  small number of instances, especially hard tardy/packing cases.
- Most large losses are preference-loss failures: third often improves `Z2` but
  loses far more in `Z3` than BRIDGE.

## Preference Seed Probe

Added an independent preference-capped seed as an experiment. It follows high
preference bays but caps per-bay block counts to avoid severe overcrowding. This
does not call BRIDGE code and remains inside the third solver.

Representative probe after adding the seed showed mixed results:

| instance | previous third | with pref-capped seed | read |
|---|---:|---:|---|
| prob_5 | 497,545 | 466,820 | improved but still far behind BRIDGE |
| prob_7 | 535,268 | 531,266 | small improvement |
| prob_16 | 985,868 | 990,178 | small regression |
| prob_17 | 1,326,736 | 1,308,317 | improvement |
| prob_20 | 2,795,902 | 2,746,428 | improvement in that run |

However, after changing the CP-SAT worker default to 1 for reproducibility, the
same seed was again mixed:

| instance | worker=1 no pref seed | worker=1 with pref seed | read |
|---|---:|---:|---|
| prob_5 | 523,158 | 523,158 | no gain |
| prob_14 | 1,557,559 | 1,533,373 | improves |
| prob_20 | 2,769,235 | 2,822,569 | regresses |

Verdict: keep `COVER_PREF_CAP_SEED=1` as an env-gated diagnostic option, not a
default. The premise is not dead, but it needs a better activation rule.

## Reproducibility

The covering MIP now defaults to `COVER_CP_WORKERS=1`. The MIP is not the runtime
bottleneck, and single-worker search keeps the evidence chain reproducible. Earlier
fixed-round ablations already used this setting; the default now matches the
experimental protocol.

## Preference-Dominant Pool Probe

Hypothesis from the BRIDGE head-to-head: the third solver is not merely choosing
badly from a rich pool; for preference-dominant/low-tardy instances, the pool lacks
enough high-preference columns. Evidence: in large losses, third often improves
`Z2` but loses much more in `Z3` than BRIDGE.

Added an env-gated preference pool branch:

- `COVER_PREF_PERTURBS_PER_ROUND=N`
- independent RNG `COVER_PREF_SEED`
- mutates assignments by moving blocks only to better-preference feasible bays
- default is off

Representative T=60 probe with `COVER_PREF_PERTURBS_PER_ROUND=6`:

| instance | default third reference | pref-pool branch | read |
|---|---:|---:|---|
| prob_3 | 249,800 | 282,220 | worse |
| prob_5 | 523,158 | 565,287 | worse |
| prob_7 | 535,268 | 533,998 | tiny improvement |
| prob_16 | 985,868 | 983,906 | tiny improvement |
| prob_17 | 1,326,736 | 1,264,933 | useful improvement |
| prob_20 | 2,769,235 | 3,258,007 | worse |

Read:

- The pool-insufficiency hypothesis is partially supported: prob_17 improves
  materially when preference columns are injected.
- It is not enough to generate preference columns indiscriminately. The branch can
  displace hard-tardy/packing progress and can worsen some low-tardy cases.
- Keep the branch env-gated. Next useful experiment would be an activation/ranking
  rule, not simply more preference perturbations.

Pool-only follow-up:

The next check was whether preference-dominant candidates should only enrich the
Set Covering pool/archive, without being allowed to directly update the incumbent.
This was tested with:

- `COVER_PREF_PERTURBS_PER_ROUND=6`
- `COVER_PREF_UPDATES_BEST=0`

| instance | default third | pref update | pref pool-only | read |
|---|---:|---:|---:|---|
| prob_3 | 249,800 | 282,220 | 295,460 | worse |
| prob_5 | 523,158 | 565,287 | 565,287 | worse |
| prob_7 | 535,268 | 533,998 | 550,796 | pool-only worse |
| prob_16 | 985,868 | 983,906 | 987,825 | no useful gain |
| prob_17 | 1,326,736 | 1,264,933 | 1,306,782 | partial gain |
| prob_20 | 2,769,235 | 3,258,007 | 3,571,655 | very bad |

Read:

- Preference-dominant columns are not merely missing as passive material. If that
  were the whole issue, pool-only enrichment should have preserved or improved the
  useful cases. It did not.
- prob_17 still supports the missing-pool hypothesis, but its best result came
  when the preference branch could affect the incumbent trajectory, not when it was
  passive pool material.
- The branch is dangerous on prob_20 and some low-tardy cases. Preference columns
  need an activation/ranking rule before they can become a default mechanism.

## Preference Auto Activation

Hypothesis:

The forced preference branch is only appropriate when the instance is already
low-tardy and `Z1` pressure is weak. In those cases, moving toward preference
basins is less likely to damage the admission schedule, while high-`w1` or
hard-tardy cases should stay with the load/tardy-oriented pool generator.

Implemented:

- `COVER_PREF_PERTURBS_PER_ROUND=auto`
- enable 6 preference perturbations/round only when:
  - `seed_tardiness_min <= COVER_PREF_TARDINESS_MAX` (default `40`)
  - `w1 < COVER_PREF_W1_MAX` (default `10000`)
- explicit env values still work:
  - `0` disables the branch
  - an integer forces that many preference perturbations per round

Targeted T=60 probe, same current code and runtime:

| instance | default before auto | pref auto | auto action | read |
|---|---:|---:|---|---|
| prob_3 | 284,030 | 284,030 | off | preserved |
| prob_5 | 523,158 | 523,158 | off | preserved |
| prob_16 | 985,868 | 983,906 | on | small improvement |
| prob_17 | 1,324,782 | 1,270,920 | on | useful improvement |
| prob_20 | 2,822,569 | 2,822,569 | off | preserved |

Read:

- The activation rule captures the two cases where forced preference search was
  useful in the previous probe, and blocks the known bad cases.
- This is a small specialist gain, not a general cure for the third solver's
  preference weakness.
- Adopt as the third solver default because it is explainable, env-controllable,
  and its changed cases were directly tested. A fresh full-20 head-to-head is
  still needed before claiming standalone competitiveness against BRIDGE.

## Current Auto Full-20 Recheck

T=60, current BRIDGE vs current third default with preference auto activation:

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| third_default vs BRIDGE | 2 | 18 | 0 | 18,316,343 vs 12,230,248 (**+49.8%**) |
| best of both, oracle choice | - | - | - | 10,868,804 vs 12,230,248 (**-11.1%**) |

third wins only on:

- prob_1: `306,504 -> 255,694`
- prob_20: `4,079,869 -> 2,769,235`

Read:

- The preference auto branch helps the known low-tardy specialist cases, but it
  does not fix the standalone replacement problem.
- The dominant failure remains `Z3`: third often lowers `Z2` but pays a much
  larger preference penalty.
- The oracle portfolio value is still real, so the third algorithm is useful as
  an independent search basin even though it is not yet a default replacement for
  BRIDGE.

## Z2/Z3 Weighted Seed

Failure signal from the full-20 recheck:

The third pool starts too load-balance-heavy. It frequently improves `Z2`, but
the missing preference structure causes very large `Z3` losses. A pure preference
branch was too blunt, so the next seed directly scalarizes the two assignment-only
terms:

`w2 * current_Z2_after_candidate + w3 * preference_loss`

Implementation:

- `_seed_z23_weighted`
- blocks are ordered by preference regret, due date, release time, and workload
- each block is assigned to the feasible bay with the best incremental weighted
  `Z2/Z3` score
- default is on
- `COVER_Z23_SEED=0` disables it for ablation

T=60 full-20 A/B against the previous current third default:

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| z23 seed vs previous third default | 8 | 7 | 5 | 16,796,595 vs 18,241,361 (**-7.9%**) |

Largest gains:

- prob_16: `983,906 -> 266,463`
- prob_11: `1,175,734 -> 614,378`
- prob_5: `523,158 -> 190,958`

Largest losses:

- prob_12: `904,522 -> 959,675`
- prob_7: `502,564 -> 544,659`
- prob_9: `282,453 -> 325,251`

T=60 against current BRIDGE after enabling z23 seed:

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| third z23 vs BRIDGE | 4 | 15 | 1 | 16,796,595 vs 12,230,248 (**+37.3%**) |
| best of both, oracle choice | - | - | - | 10,314,963 vs 12,230,248 (**-15.7%**) |

Read:

- Adopt z23 seed by default inside the third solver. It is not uniformly better,
  but the aggregate gain is large and the mechanism directly targets the measured
  `Z3` failure.
- Standalone third is still behind BRIDGE, but the gap narrowed from +49.8% to
  +37.3%.
- The oracle portfolio improved from -11.1% to -15.7%, which strengthens the
  case that third should keep exploring a genuinely different basin.

## Final Portfolio Recheck

Question:

After z23 seed changed the pool, should final candidate selection revisit source
diversity? The earlier source portfolio was rejected because it displaced strong
proxy-ranked candidates.

Implemented env-gated `COVER_FINAL_PORTFOLIO=hybrid`:

- `proxy`: score proxy top `COVER_FINAL_CANDIDATES` only
- `source`: old source-bucket mode
- `hybrid`: score proxy top candidates first, then add up to
  `COVER_FINAL_SOURCE_CANDIDATES` source-bucket candidates

Targeted T=60 current-code probe on cases where ranking/source diversity might
matter:

| mode vs proxy | wins | losses | ties | aggregate on prob_5/6/7/9/10/12/16/17/19/20 |
|---|---:|---:|---:|---:|
| source | 3 | 4 | 3 | 9,387,895 vs 9,474,746 (**-0.9%**) |
| hybrid | 2 | 4 | 4 | 9,451,874 vs 9,474,746 (**-0.2%**) |

Read:

- Ranking/source diversity is a real but small effect.
- `source` and `hybrid` are not clean defaults: both can regress individual
  cases, and the aggregate improvement is much smaller than z23 seed.
- Keep default `proxy`; keep `source`/`hybrid` as env-gated diagnostics.

## Preference W1 Threshold Probe

Question:

The preference auto branch currently enables only when `seed_tardiness_min <= 40`
and `w1 < 10000`. This protects high-Z1 cases, but may be too narrow for
preference-loss instances such as prob_15/prob_18/prob_19.

Probe:

- current z23 default
- compare default `COVER_PREF_W1_MAX=10000` vs `COVER_PREF_W1_MAX=20000`
- targeted T=60 on prob_3/7/15/18/19/20

| instance | default | w1 max 20000 | preference branch action |
|---|---:|---:|---|
| prob_3 | 309,210 | 284,030 | off in both |
| prob_7 | 544,659 | 550,796 | enabled only at 20000 |
| prob_15 | 1,141,963 | 1,137,397 | enabled only at 20000 |
| prob_18 | 1,385,457 | 1,344,593 | enabled only at 20000 |
| prob_19 | 1,287,114 | 1,269,533 | enabled only at 20000 |
| prob_20 | 2,746,428 | 2,826,429 | off in both |

Active cases only:

- 3 wins, 1 loss
- aggregate `4,302,319 vs 4,359,193` (**-1.3%**)

All targeted cases:

- 4 wins, 2 losses
- aggregate essentially flat: `7,412,778 vs 7,414,831` (**-0.03%**)

Read:

- Raising the threshold is directionally interesting on active cases, but the
  evidence is too small and noisy to change the default.
- Keep `COVER_PREF_W1_MAX=10000` as the default.
- Next useful version would need a better activation signal than `w1` alone,
  likely involving current `Z3` loss or preference regret concentration.

## Z2/Z3 Branch

Question:

z23 seed improved the starting pool, but the ILS-like pool regeneration still
mostly used load-biased perturbation plus optional preference-only perturbation.
The next test was whether a cheap assignment-only z23 local branch can keep
generating pool columns in the same objective basin.

Implementation:

- `_mutate_z23`
- samples high preference-loss blocks
- tries feasible bay moves and accepts moves that improve assignment-only
  `w2*Z2 + w3*Z3`
- final acceptance still uses the full pool proxy, then final best-of packing
- default is now `COVER_Z23_PERTURBS_PER_ROUND=auto`
- auto enables 2 z23 perturbations/round when
  `seed_tardiness_min <= COVER_Z23_BRANCH_TARDINESS_MAX` (default was `100`
  at this stage; later raised to `250` in the hard-tardy threshold section)
- `COVER_Z23_PERTURBS_PER_ROUND=0` disables it

Targeted T=60 probe on known z23-loss and z23-gain cases:

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| z23 branch2 vs no branch | 8 | 2 | 0 | 9,620,938 vs 10,212,786 (**-5.8%**) |
| z23 branch auto vs no branch | 8 | 2 | 0 | 9,431,861 vs 10,212,786 (**-7.6%**) |

Full-20 T=60, comparing z23 branch auto to z23 seed only:

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| z23 branch auto vs z23 seed only | 14 | 2 | 4 | 14,965,641 vs 16,796,595 (**-10.9%**) |

Against current BRIDGE:

| comparison | wins | losses | ties | aggregate objective |
|---|---:|---:|---:|---:|
| third z23 branch auto vs BRIDGE | 6 | 13 | 1 | 14,965,641 vs 12,230,248 (**+22.4%**) |
| best of both, oracle choice | - | - | - | 10,082,794 vs 12,230,248 (**-17.6%**) |

Largest gains over z23 seed only:

- prob_17: `1,270,920 -> 715,106`
- prob_10: `850,835 -> 643,677`
- prob_18: `1,366,576 -> 1,180,382`
- prob_15: `1,141,963 -> 965,978`
- prob_11: `614,378 -> 462,858`

Losses:

- prob_12: `959,675 -> 1,153,043`
- prob_20: `2,746,428 -> 2,799,762`

Pool-only follow-up:

- `COVER_Z23_UPDATES_BEST=0` protects prob_12 in a small probe, but loses much
  of the key prob_17/prob_5 benefit.
- The branch is not merely useful as passive pool material; it needs to move the
  search trajectory to unlock the large gains.

Read:

- Adopt z23 branch auto as the third solver default.
- This is the strongest post-seed improvement so far, and directly addresses the
  measured `Z3` failure mode.
- The main remaining weakness is now narrower: protecting hard/tardy cases like
  prob_12/prob_20 while keeping the z23 branch's preference-recovery gains.

## Pool Source / Final Materialization Diagnostic

Question:

The user hypothesis was that preference-dominant low-tardy cases may still lack
enough pool material.  After z23 branch adoption, the sharper question is whether
the pool is missing the material, or whether generated source buckets fail to
survive the final true-score materialization step.

Implementation:

- Added diagnostic-only stats:
  - `archive_source_counts`
  - `proxy_top_sources`
  - `final_source`
  - `COVER_DIAG_SOURCE=1` source-bucket true-score probe
- Added `COVER_FINAL_SCORE_GUARD` so final candidate scoring can use the packed
  solution it already builds instead of leaving an overly conservative idle tail.
- Avoid re-scoring the already materialized search-best assignment in the final
  portfolio.
- Added env-gated `COVER_FINAL_PORTFOLIO=interleave`, which alternates proxy-top
  candidates with source-bucket representatives.  It is a diagnostic option, not
  the default.

Source diagnostic, T=45:

| instance | variant | emitted final | best source true | best source | read |
|---|---:|---:|---:|---|---|
| prob_12 | default | 1,176,164 | 1,143,838 | z23 | candidate existed, final proxy missed it |
| prob_12 | z23 off | 1,015,956 | 994,038 | perturb_improve | candidate existed, final proxy missed it |
| prob_12 | z23 pool-only | 985,006 | 905,735 | z23_improve | passive pool can be good here |
| prob_17 | default | 889,809 | 831,627 | z23_improve | z23 branch creates the right basin |
| prob_17 | z23 off | 1,293,126 | 1,283,429 | perturb_improve | no z23 basin, much worse |
| prob_17 | z23 pool-only | 1,249,839 | 1,205,299 | z23_improve | pool-only still far behind update-on |

Read:

- The hypothesis is partly right, but not as "more pool volume".  The missing
  piece is source-directed low-tardy material plus a final materialization path
  that actually scores those source buckets.
- prob_17 confirms that passive pool enrichment is not enough; z23 must be able
  to move the incumbent/search trajectory.
- prob_12 confirms a separate ranking/materialization failure: useful z23-family
  candidates are already in the archive, but proxy-only final selection can miss
  them.

Final portfolio recheck after z23 branch and final-score changes:

`COVER_FINAL_PORTFOLIO=interleave` vs default proxy, T=45:

| probe set | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| prob_5/12/17/20 | 1 | 0 | 3 | 4,887,377 vs 4,919,703 (**-0.7%**) |
| prob_6/7/10/15/18/19 | 1 | 2 | 3 | 5,620,954 vs 5,517,491 (**+1.9%**) |
| combined | 2 | 2 | 6 | 10,508,331 vs 10,437,194 (**+0.7%**) |

Read:

- `interleave` solves the prob_12 miss and preserves prob_5/prob_17/prob_20 in
  the first probe, but it regresses prob_18 and prob_19.
- Do not adopt `interleave` as default.  Keep it env-gated for diagnostics and
  future portfolio design.

Final score guard check, default proxy, T=45:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| `COVER_FINAL_SCORE_GUARD=0.75` vs `3.0` | 2 | 0 | 3 | 6,144,795 vs 6,426,179 (**-4.4%**) |

Per-instance:

- prob_5: 165,484 vs 165,484
- prob_12: 1,176,164 vs 1,176,164
- prob_17: 831,627 vs 831,627
- prob_18: 1,225,092 vs 1,319,807
- prob_20: 2,746,428 vs 2,933,097

Decision:

- Keep default final portfolio as `proxy`.
- Keep `COVER_FINAL_PORTFOLIO=interleave` env-gated only.
- Adopt the final materialization oiling:
  - skip duplicate scoring of the already materialized search-best assignment
  - default `COVER_FINAL_SCORE_GUARD=0.75`
- This is not a new search heuristic; it is a safer use of the already generated
  pool/archive under the true best-of materialization oracle.

## Current Default Full20 Recheck

Question:

After the final-score guard/materialization oiling, re-run the current default
third solver on all 20 instances at T=60 to learn whether the improvement holds
outside the targeted probes.

Result:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| current default vs previous z23 branch auto | 7 | 0 | 13 | 14,676,207 vs 14,965,641 (**-1.9%**) |
| current default vs BRIDGE | 6 | 13 | 1 | 14,676,207 vs 12,230,248 (**+20.0%**) |
| best of current default and BRIDGE | - | - | - | 9,894,535 vs 12,230,248 (**-19.1%**) |

Read:

- The materialization oiling is a real improvement and caused no loss against
  the previous z23-branch baseline.
- The standalone third solver is still behind BRIDGE on aggregate.
- The oracle value improved slightly again, so the third solver remains strongly
  complementary.
- Remaining large losses are mostly low-tardy/high-Z3 cases: prob_19, prob_15,
  prob_12, prob_17, prob_10.

## Guarded Wide Preference Activation

Question:

The remaining failures are often nearly solved on Z1 but weak on Z3.  The old
preference branch only enabled for `seed_tardiness_min <= 40` and `w1 < 10000`,
which misses prob_10/prob_12/prob_15/prob_18/prob_19.  Test whether a wider
preference branch can repair those cases without regressing the cases where
wide preference previously hurt.

Target T=45 probe, update-on wide preference:

| variant | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| wide20/tardy100 vs default | 3 | 2 | 1 | 5,565,461 vs 5,722,320 (**-2.7%**) |
| wide30/tardy100 vs default | 3 | 3 | 0 | 5,406,768 vs 5,722,320 (**-5.5%**) |

Key reads:

- wide30 fixes prob_12 (`1,176,164 -> 1,040,357`) and wide20/30 both fix
  prob_10 and prob_19.
- wide preference is not safe as a blanket default: prob_15 and prob_18 regress,
  and prob_7 is noisy.

Pool-only follow-up:

| variant | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| wide20 pool-only vs default | 1 | 4 | 1 | 5,639,530 vs 5,722,320 (**-1.4%**) |
| wide30 pool-only vs default | 2 | 4 | 0 | 5,503,723 vs 5,722,320 (**-3.8%**) |

Read:

- Pool-only protects neither enough nor consistently.  The branch needs to move
  search state in the winning cases, similar to the z23 branch lesson.

Guarded rule:

Enable the wider preference branch only when:

- `seed_tardiness_min <= 100`
- `w1 < 30000`
- and either `seed_tardiness_min > 40` or `w1 <= 11000`

This is available as `COVER_PREF_PROFILE=wide_guarded`, and is now the default.
Use `COVER_PREF_PROFILE=basic` to restore the previous narrow activation.

Target T=45 guarded result:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| wide_guarded vs default | 4 | 0 | 2 | strong targeted improvement |

Full20 T=60 guarded result:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| wide_guarded vs current default | 4 | 1 | 15 | 14,223,987 vs 14,676,207 (**-3.1%**) |
| wide_guarded vs BRIDGE | 7 | 12 | 1 | 14,223,987 vs 12,230,248 (**+16.3%**) |
| best of wide_guarded and BRIDGE | - | - | - | 9,892,171 vs 12,230,248 (**-19.1%**) |

Largest improvements vs current default:

- prob_19: `1,134,081 -> 913,058`
- prob_12: `1,153,043 -> 957,759`
- prob_10: `635,109 -> 488,033`
- prob_5: `181,018 -> 152,062`

Loss:

- prob_18: `1,121,632 -> 1,261,751`

Repeat check on prob_18:

- guarded profile did not activate (`pref_perturbs_per_round=0`)
- repeated paired runs showed the difference is deadline/run variance, not a
  preference-branch logic change

Decision:

- Adopt `wide_guarded` as the third solver default.
- Keep the old behavior available through `COVER_PREF_PROFILE=basic`.
- Keep monitoring prob_18-style variance; do not add another rule from that one
  noisy loss yet.

## Hard-Tardy Z23 Threshold

Question:

After guarded preference activation, the largest remaining non-preference hole is
prob_13.  It has `seed_tardiness_min=217`, so the old z23 branch threshold
(`100`) disables z23 even though the instance still has a large assignment-space
gap.

Targeted prob_13 T=60 controls:

| variant | objective | Z1 | read |
|---|---:|---:|---|
| default | 1,739,506 | 29 | z23/shadow off, MIP does not update incumbent |
| MIP update max 300 | 1,739,506 | 29 | no effect |
| z23 max 300 | 1,321,194 | 8 | large improvement, beats BRIDGE on prob_13 |
| shadow max 300 | 1,687,260 | 27 | small improvement only |
| hard combo | 1,835,102 | 36 | harmful |

Risk check:

- `z23 max 300` hurts prob_20: `2,639,760 -> 3,061,357`
- Current seed tardiness signals:
  - prob_13: `217`
  - prob_14: `307`
  - prob_20: `292`

Decision:

- Set default `COVER_Z23_BRANCH_TARDINESS_MAX=250`.
- This enables z23 on prob_13 while keeping prob_20 protected.
- Keep `shadow` and MIP update thresholds unchanged.

Targeted verification after default change:

| instance | objective | z23 perturbs | read |
|---|---:|---:|---|
| prob_13 | 1,321,194 | 2 | z23 enabled, improvement reproduced |
| prob_20 | 2,639,760 | 0 | z23 remains off, protection preserved |

Full20 T=60 after z23 threshold 250:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| current default vs guarded previous | 4 | 2 | 14 | 13,781,899 vs 14,223,987 (**-3.1%**) |
| current default vs BRIDGE | 8 | 11 | 1 | 13,781,899 vs 12,230,248 (**+12.7%**) |
| best of current default and BRIDGE | - | - | - | 9,837,157 vs 12,230,248 (**-19.6%**) |

Notes:

- Some unaffected instances varied between full20 runs (notably prob_10), so the
  exact aggregate should be read with normal time-limit variance in mind.
- The prob_13 improvement itself is structural and repeated under the targeted
  control.
- This is the first hard-tardy improvement that did not rely on BRIDGE behavior.

## Adaptive Z23 Branch Count

Question:

After raising the z23 tardiness threshold, the remaining large losses are still
mostly low-tardy/high-Z3 cases.  The z23 branch is already useful, but default
auto used only 2 z23 perturbations per round.  Test whether stronger z23
pressure helps, and whether hard-tardy cases need protection.

Implementation:

- Added `COVER_Z23_AUTO_COUNT`.
- Default is now `adaptive`:
  - if `seed_tardiness_min <= COVER_Z23_COUNT6_TARDINESS_MAX` (default `100`),
    use 6 z23 perturbations per round
  - otherwise use 2 while still inside `COVER_Z23_BRANCH_TARDINESS_MAX`
- Explicit integer values still force a fixed count.

Target T=45 count probe:

| variant | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| z23 count 4 vs default | 3 | 5 | 1 | 10,181,442 vs 10,342,761 (**-1.6%**) |
| z23 count 6 vs default | 7 | 1 | 1 | 9,274,016 vs 10,342,761 (**-10.3%**) |

Read:

- Count 4 is not reliable.
- Count 6 is very strong on low/mid tardiness high-Z3 cases.
- The single clear count6 loss is prob_13, where hard-tardy z23 should stay at
  count 2.

Target T=60 adaptive check on prob_7/10/12/13/15/17/18/19/20:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| adaptive count vs previous default | 7 | 1 | 1 | 8,490,761 vs 9,565,243 (**-11.2%**) |

Key checks:

- prob_13 remains protected at count 2: `1,321,194 -> 1,321,194`
- prob_20 remains z23-off; observed `2,629,509 -> 2,639,760` is normal
  time-limit variance with the same branch decision
- largest wins: prob_15, prob_19, prob_10, prob_12, prob_17

Full20 T=60 after adaptive count:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| adaptive count vs previous default | 10 | 7 | 3 | 13,004,544 vs 13,781,899 (**-5.6%**) |
| adaptive count vs BRIDGE | 6 | 13 | 1 | 13,004,544 vs 12,230,248 (**+6.3%**) |
| best of adaptive count and BRIDGE | - | - | - | 10,092,853 vs 12,230,248 (**-17.5%**) |

Read:

- This is the largest standalone improvement since the z23 branch was introduced.
- It trades some complementary oracle value for much stronger standalone score.
- Losses concentrate in some higher `w3` or already-good cases
  (prob_4/prob_6/prob_9/prob_11), so the next likely refinement is a second
  guard on when count6 should activate.
- A simple retrospective simulation that keeps count6 only for `w3 <= 133`
  would improve the full20 aggregate further (`13,004,544 -> 12,833,872`), but
  this is not yet adopted because it has not been run end-to-end.

Decision:

- Adopt adaptive z23 auto count as the third solver default.
- Keep `COVER_Z23_AUTO_COUNT=2`, `4`, or `6` available for ablations.
- Keep `COVER_Z23_COUNT6_TARDINESS_MAX` env-tunable; default `100`.

## Z23 Count6 W3 Guard

Question:

Adaptive count6 improved standalone score, but it also introduced losses in some
`w3=150/200` cases.  The full20 deltas by `w3` showed:

- `w3=133`: large net gain despite one loss
- `w3=150/200`: mixed and slightly harmful in aggregate

Test whether count6 should be guarded by the Z3 weight class.

Implementation:

- Added `COVER_Z23_COUNT6_W3_MAX`.
- In adaptive mode, count6 now requires both:
  - `seed_tardiness_min <= COVER_Z23_COUNT6_TARDINESS_MAX`
  - `w3 <= COVER_Z23_COUNT6_W3_MAX`
- Default `COVER_Z23_COUNT6_W3_MAX=133`.

Target T=45 guard probe on affected cases:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| w3 guard vs adaptive count | 5 | 2 | 6 | 7,161,046 vs 7,168,938 (**-0.1%**) |

Read:

- The guard protects prob4/prob6/prob9 but loses prob1/prob3 count6 gains.
- Target result is close, so full20 confirmation is necessary.

Full20 T=60 with w3 guard:

| comparison | wins | losses | ties | aggregate |
|---|---:|---:|---:|---:|
| w3 guard vs adaptive count | 6 | 3 | 11 | 12,826,333 vs 13,004,544 (**-1.4%**) |
| w3 guard vs threshold250 previous | 7 | 2 | 11 | 12,826,333 vs 13,781,899 (**-6.9%**) |
| w3 guard vs BRIDGE | 8 | 11 | 1 | 12,826,333 vs 12,230,248 (**+4.9%**) |
| best of w3 guard and BRIDGE | - | - | - | 10,004,908 vs 12,230,248 (**-18.2%**) |

Decision:

- Adopt the w3 guard as default.
- This gives up some count6 wins on high-w3 cases, but improves the full20
  standalone aggregate and reduces the BRIDGE gap again.
- Current third solver is now close enough to BRIDGE on aggregate that remaining
  work should focus on selective protection/portfolio rather than broad branch
  amplification.

## Final Portfolio Recheck After W3 Guard

Question:

Earlier `COVER_FINAL_PORTFOLIO=interleave` was mixed.  After the adaptive z23
count and w3 guard changed the archive/source composition, re-test whether final
true-score materialization should protect source diversity by default.

Full20 T=60, current w3-guarded default, one process per mode:

| final portfolio | wins | losses | ties | aggregate vs proxy |
|---|---:|---:|---:|---:|
| `source` | 8 | 4 | 8 | 12,682,579 vs 12,647,837 (**+0.3%**) |
| `interleave` | 7 | 0 | 13 | 12,446,587 vs 12,647,837 (**-1.6%**) |

Key per-instance reads:

- `source` is still unsafe: it improves several preference/Z23 cases, but loses
  badly on prob_20 and also regresses prob_6/prob_14/prob_18.
- `interleave` preserved the proxy result everywhere in this run and recovered
  missed source-bucket candidates on prob_1/prob_3/prob_8/prob_10/prob_12/
  prob_16/prob_20.
- This supports the current failure diagnosis: useful low-tardy or source-directed
  candidates often exist in the archive, but proxy-only final scoring can miss
  them.  The fix is not broad passive pool volume; it is a protected final
  materialization path that scores a small source-diverse slice by the true
  best-of objective.

Preference-pool interpretation:

- The "preference-dominant low-tardy pool is insufficient" hypothesis remains
  partially true, but only in a narrow form.
- Previous forced preference-pool and pool-only probes showed that simply adding
  more preference candidates can regress hard-tardy and some already-good cases.
- The safer pattern is: generate preference/Z23 candidates only under guards,
  allow the winning branches to move the incumbent when evidence supports it,
  and make final scoring source-aware enough that useful candidates are not
  discarded by the AABB proxy rank.

Decision:

- Adopt `COVER_FINAL_PORTFOLIO=interleave` as the third solver default.
- Keep `COVER_FINAL_PORTFOLIO=proxy`, `source`, and `hybrid` available for
  ablations and rollback.
- Next useful diagnostic is source-wise true-score/Pareto slicing for low-tardy
  cases: determine whether low `Z1` + low `Z3` candidates are absent from the
  archive or present but filtered by final ranking.

## Source-Wise Pareto Diagnostic

Question:

The remaining preference/low-tardy suspicion is ambiguous: are good candidates
missing from the pool, or present in the archive but missed by final source/proxy
ranking?

Implementation:

- Added env-gated `COVER_DIAG_SOURCE_PARETO=N`.
- The diagnostic scans the archive cheaply by AABB `Z1` and exact `Z2/Z3`, then
  true-scores a small source-wise slice with the same best-of AABB/polygon scoring
  used by final materialization.
- Added `final_obj1/final_obj2/final_obj3` to `LAST_STATS` for diagnosis.

Target T=60 diagnostic:

| instance | final | archive signal | read |
|---|---:|---:|---|
| prob_10 | 376,397 | no lower-`Z3` low-`Z1` candidate found | not a final-ranking miss |
| prob_12 | 821,783 | z23_improve candidate true-scores 812,823 | source bucket internal ranking miss |
| prob_15 | 664,084 | no lower-`Z3` low-`Z1` candidate found | preference pool not the visible issue |
| prob_16 | 189,862 | z23_improve candidate true-scores 188,849 | small source bucket ranking miss |
| prob_18 | 1,050,927 | no lower-`Z3` low-`Z1` candidate found | not a final-ranking miss |

Read:

- The hypothesis is now narrower: useful candidates sometimes exist, but not
  broadly enough to justify more preference-pool volume.
- The concrete miss is inside source buckets, especially z23-family candidates:
  proxy-best within a source group is not always true-best.
- AABB `Z1` can overestimate true best-of `Z1`: prob_12 had no cheap
  `z1_aabb <= final_z1 + 2` promising row, yet true scoring found candidates
  with the same true `Z1` and better `Z3`.

## Source Per Group Top-K Probe

Question:

If source bucket internal ranking misses exist, should `interleave` score more
than one candidate per source group?

Implementation:

- Added env-gated `COVER_FINAL_SOURCE_PER_GROUP`.
- Default remains `1`.
- First implementation naively took top-k from combined source groups.

Immediate counterexample:

| variant | prob_10 objective | read |
|---|---:|---|
| k1 default | 376,397 | current default |
| naive k3 | 384,328 | worse; z23_improve entries displaced the useful z23 representative |

The implementation was adjusted to preserve all k1 representatives first and
only then add extra candidates.  The same prob_10 guard still failed:

| variant | prob_10 objective | archive size | read |
|---|---:|---:|---|
| k1 default | 376,397 | 501 | current default |
| preserved-k3 | 501,816 | 411 | worse; extra final scoring/time sensitivity changed the run |

Decision:

- Do not adopt source top-k as a default.
- Keep `COVER_FINAL_SOURCE_PER_GROUP` env-gated only for diagnostics.
- The next safe direction is not broad top-k scoring.  If this is revisited, it
  needs a stricter non-regression design, such as scoring an extra candidate only
  when enough final slack remains and after all current k1 candidates have already
  been scored.

Fixed-round isolation:

To separate final-selection behavior from timed search cutoff, reran k1 vs k3
with `COVER_MAX_ROUNDS=20` and `T=120` on prob_10/prob_12/prob_16.

| instance | k1 objective | k3 objective | archive | read |
|---|---:|---:|---:|---|
| prob_10 | 501,816 | 501,816 | 411 | same archive, no k3 gain |
| prob_12 | 762,452 | 762,452 | 400 | same archive, no k3 gain |
| prob_16 | 161,169 | 161,169 | 486 | same archive, no k3 gain |

In all three cases k3 only increased final true-score checks:

- prob_10: 9 -> 17
- prob_12: 9 -> 15
- prob_16: 9 -> 17

Interpretation:

- The timed prob_10 k3 regression came from time/cutoff sensitivity changing the
  search archive, not from k3 selecting a worse candidate from the same archive.
- But the fixed-round oracle also shows no upside from broad k3 scoring on these
  cases.  It spends polygon/materialization budget without improving the final
  result.
- This strengthens the decision to keep source top-k diagnostic-only.

## Time Scaling And CP Cap Probe

Question:

The actual evaluation time limit may vary widely.  Check whether the third
solver's current budget split scales sensibly with time, and whether the covering
MIP spends too much per round.

Implementation:

- Added timing stats:
  - `elapsed_total`
  - `elapsed_search_phase`
  - `elapsed_final_phase`
  - `search_budget`
  - `poly_budget`
  - `build_reserve`
  - `stopped_by_round_cap`
- Added env-gated `COVER_CP_TIME_CAP`, default `2.5`, to test shorter covering
  MIP calls without changing default behavior.

Time scaling probe, current default, prob_10/prob_12/prob_16:

| instance | T | objective | rounds | archive | search phase | final phase |
|---|---:|---:|---:|---:|---:|---:|
| prob_10 | 30 | 599,262 | 11 | 247 | 24.0s | 2.6s |
| prob_10 | 60 | 502,768 | 20 | 395 | 50.4s | 4.7s |
| prob_10 | 90 | 479,143 | 26 | 514 | 75.6s | 5.8s |
| prob_12 | 30 | 892,024 | 10 | 216 | 24.0s | 2.3s |
| prob_12 | 60 | 812,610 | 18 | 353 | 50.6s | 4.8s |
| prob_12 | 90 | 809,026 | 25 | 477 | 75.6s | 8.7s |
| prob_16 | 30 | 247,647 | 11 | 282 | 24.0s | 1.2s |
| prob_16 | 60 | 166,546 | 19 | 446 | 50.4s | 4.6s |
| prob_16 | 90 | 140,481 | 25 | 590 | 75.6s | 8.4s |

Read:

- The budget split scales as intended: search uses almost exactly the reserved
  search budget, and final materialization stays inside the tail.
- More time generally improves the result, especially from 30s to 60s; 60s to
  90s is smaller and instance dependent.
- Final scoring is not the dominant scheduler bottleneck in these runs.  The
  important driver is how many useful pool/covering rounds fit into the search
  phase.

CP cap A/B, T=60:

| instance | cap 2.5s | cap 1.0s | rounds 2.5 -> 1.0 | read |
|---|---:|---:|---:|---|
| prob_10 | 502,768 | 375,823 | 20 -> 26 | cap1 helps a lot |
| prob_12 | 762,452 | 814,227 | 18 -> 23 | cap1 hurts |
| prob_16 | 166,546 | 160,160 | 19 -> 25 | cap1 helps |

Decision:

- Keep default `COVER_CP_TIME_CAP=2.5`.
- Keep `COVER_CP_TIME_CAP` env-gated for future targeted tuning.
- Do not adopt a global lower cap: more rounds are valuable on some instances,
  but prob_12 shows the covering MIP sometimes needs the fuller solve time to
  produce a better anchor.
- Next useful scheduling experiment would need an activation rule, not a blanket
  cap reduction.  A plausible cheap rule is to test lower CP caps only for lower
  `w1` / already low-tardy cases, but this is not yet validated.

Follow-up activation probe:

Tested `COVER_CP_TIME_CAP=1.0` on low-`w1` / low-tardy candidates plus controls.

| instance | cap 2.5s | cap 1.0s | rounds 2.5 -> 1.0 | branch signal | read |
|---|---:|---:|---:|---|---|
| prob_5 | 172,881 | 166,930 | 27 -> 39 | pref on, z23=2 | win |
| prob_8 | 11,252 | 11,252 | 29 -> 40 | pref on, z23=2 | tie |
| prob_9 | 192,222 | 300,309 | 18 -> 24 | pref on, z23=2 | large loss |
| prob_10 | 502,768 | 375,823 | 20 -> 26 | pref on, z23=6 | large win |
| prob_12 | 770,432 | 814,227 | 18 -> 23 | pref on, z23=6 | control loss |
| prob_15 | 613,470 | 831,813 | 17 -> 22 | pref off, z23=6 | large loss |
| prob_16 | 166,546 | 163,932 | 19 -> 24 | pref on, z23=6 | small win |
| prob_17 | 589,319 | 546,787 | 15 -> 20 | pref on, z23=6 | win |
| prob_18 | 937,779 | 1,123,650 | 15 -> 19 | pref off, z23=6 | large loss |
| prob_19 | 664,834 | 618,854 | 15 -> 19 | pref on, z23=6 | win |

Aggregate on this probe: 5 wins / 4 losses / 1 tie, but cap1 is worse overall
(`4,953,577` vs `4,621,503`, **+7.2%**) because the losses are large.

Read:

- Lower CP cap is a real lever: it consistently increases rounds/archive size.
- It is not safely predicted by `w1 <= 16000` or by `pref_perturbs_per_round > 0`;
  prob_9 breaks that rule badly.
- Losses cluster when the faster covering MIP sends search toward a worse basin
  despite more rounds.  The branch signal alone is therefore too crude.

Decision:

- Do not adopt adaptive CP cap yet.
- Keep the env knob and timing stats.
- A future rule needs a stronger diagnostic, likely based on early-round response
  or incumbent improvement rate rather than static weights alone.
