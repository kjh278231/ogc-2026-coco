# solve_bay Placement Improvement Experiment Design

## 0. Purpose

현재 solver의 큰 구조는 이미 검증되어 있다.

- outer search는 assignment를 바꾼다.
- inner `solve_bay()`는 fixed bay + fixed block set을 빠르게 배치하고 `Z1`을 만든다.
- `total_obj()`는 `solve_bay()` 결과와 `Z2/Z3`를 합쳐 assignment를 평가한다.

이번 실험의 목적은 outer search 자체를 바꾸기 전에, `solve_bay()` 내부의 greedy 배치 선택을 거의 같은 비용으로 더 좋게 만들 수 있는지 검증하는 것이다.

핵심 질문:

> 같은 block set과 같은 bay에서, 계산 속도 손실을 작게 유지하면서 평균 tardiness를 낮추는 placement rule이 있는가?

비목표:

- 병렬 portfolio로 여러 해를 동시에 돌리는 것.
- full lookahead / MIP / heavy polygon optimization으로 `solve_bay()`를 근본적으로 느리게 만드는 것.
- 단일 사례 개선을 근거로 default를 바꾸는 것.

이번 실험은 "항상 개선"이 아니라 "평균적으로 개선"을 목표로 한다. 따라서 반드시 regression을 계량하고, 어떤 instance family에서 손해가 나는지도 함께 기록한다.

## 1. Current Baseline

현재 `solve_bay()`의 per-bay packing은 다음 greedy rule이다.

```text
for block in sorted(ids, key=(due_date, processing_time)):
    for t from release_time forward:
        find first feasible slot
```

slot finder의 baseline 특성:

- orientation은 입력 순서대로 본다.
- coordinate scan은 row-major bottom-left first-fit이다.
- AABB-disjoint를 먼저 검사한다.
- `SOLVER_MASK_SEARCH=1`이면 AABB 실패 시 mask slot finder로 rescue한다.
- final build는 `best-of(AABB, mask)` 형태로 per-bay tardiness가 낮은 pack을 선택한다.

이 baseline은 매우 빠르고 feasible 안정성이 높지만, 다음 선택은 거의 최적화하지 않는다.

- block insertion order
- orientation order
- 같은 시간 `t`에서 여러 feasible slot 중 어느 slot이 미래 공간을 덜 망치는지
- AABB가 이미 slot을 찾은 경우, 그보다 앞쪽에 mask로 가능한 tighter placement가 있는지

## 2. Main Hypothesis

`solve_bay()`의 비용 대부분은 candidate scan과 collision check다. 따라서 candidate space를 크게 늘리는 변경은 위험하다.

평균 개선 가능성이 높은 변경은 다음 조건을 만족해야 한다.

- 기존과 같은 수의 block을 배치한다.
- orientation/candidate/time scan을 대폭 늘리지 않는다.
- greedy decision의 tie-break 또는 order만 바꾼다.
- fixed assignment 기준으로 `sum_j T_j`가 줄어드는지 먼저 본다.

작업 가설:

> EDD-only order와 input orientation order는 geometry 정보를 버린다. Slack, footprint difficulty, bbox compactness를 tie-break로 넣으면 거의 같은 비용으로 평균 tardiness가 내려갈 수 있다.

## 3. Candidate Variants

### V0. Baseline

현재 default와 동일하다.

```text
block order      = (due_date, processing_time)
orientation      = input order
slot choice      = first feasible bottom-left
mask behavior    = AABB fail -> mask rescue
```

모든 실험의 기준선이다.

### V1. Slack + Geometry Block Order

block order를 EDD에서 slack-aware order로 바꾼다.

추천 key:

```text
latest_start = due_date - processing_time

key = (
    latest_start,
    due_date,
    release_time,
    -bbox_area_best_fit,
    -workload
)
```

의도:

- `due_date`만 빠른 block이 아니라 실제로 늦게 시작하면 위험한 block을 앞에 둔다.
- 비슷하게 급한 block끼리는 큰 block 또는 배치 어려운 block을 먼저 놓는다.
- 큰 block을 나중에 넣어서 공간이 잘게 쪼개진 뒤 실패하는 일을 줄인다.

계산 비용:

- per-bay sort key 계산만 추가된다.
- `solve_bay()` candidate scan 횟수 자체는 거의 변하지 않는다.

위험:

- 작은 urgent block을 먼저 넣어야 하는 instance에서는 큰 block 우선 tie-break가 손해일 수 있다.
- `Z1`이 줄어든 assignment를 search가 선호하면서 `Z2/Z3`가 악화될 수 있다.

### V2. Geometry-Aware Orientation Order

orientation을 입력 순서가 아니라 bay와 shape에 맞게 정렬한다.

추천 score:

```text
bbox_w, bbox_h = orientation bbox size
bbox_area = bbox_w * bbox_h
aspect_error = abs((bbox_w / bbox_h) - (bay_width / bay_height))
density = footprint_area / bbox_area

key = (
    bbox_area,
    aspect_error,
    -density
)
```

의도:

- AABB/mask 기반 packing에서 bbox가 작고 조밀한 orientation을 먼저 시도한다.
- bay의 aspect ratio와 맞는 orientation을 먼저 써서 strip fragmentation을 줄인다.

계산 비용:

- orientation 수는 동일하다.
- order만 바뀌므로 candidate scan 규모는 거의 같다.
- footprint density를 쓰려면 Shapely footprint area가 필요할 수 있으므로, 첫 실험에서는 `bbox_area + aspect_error`만 사용하고 density는 별도 variant로 둔다.

위험:

- bbox가 작은 orientation이 실제로는 나중 block의 핵심 공간을 자를 수 있다.
- input orientation order가 문제 생성 방식상 이미 좋은 순서일 가능성도 있다.

### V3. Tiny Slot Scoring, K=3

현재는 첫 feasible slot을 즉시 채택한다. V3는 같은 time `t`에서 처음 발견한 feasible candidate K개만 모아 cheap score로 고른다.

초기 K:

```text
K = 3
```

cheap score 후보:

```text
score =
    + envelope_growth
    + thin_strip_penalty
    - wall_contact_bonus
    - block_contact_bonus
```

정의:

- `envelope_growth`: 현재 temporally-overlapping placed blocks의 bbox union envelope가 얼마나 커지는지.
- `thin_strip_penalty`: candidate를 놓고 남는 좌/우/상/하 strip 중 너무 얇은 영역을 만드는 penalty.
- `wall_contact_bonus`: bay wall에 붙으면 bonus.
- `block_contact_bonus`: 기존 bbox와 edge가 맞닿으면 bonus.

의도:

- 중앙을 자르는 placement를 피하고, 벽/기존 block에 붙여 fragmentation을 줄인다.

계산 비용:

- worst case candidate scan이 K배까지 늘 수 있다.
- 다만 K를 3으로 제한하고, `t`별 첫 feasible 근처만 보므로 통제 가능하다.

위험:

- score가 잘못 설계되면 first-fit보다 더 나쁠 수 있다.
- search scoring에 바로 넣으면 landscape가 바뀌어 outer search regression이 생길 수 있다.

### V4. Mask-Prefix Rescue

현재 mask는 AABB가 slot을 못 찾을 때만 사용된다. V4는 AABB first slot을 fallback으로 잡고, 그보다 앞쪽 prefix에서 mask로 가능한 tighter slot을 제한적으로 찾는다.

정책:

```text
1. baseline AABB first slot을 찾는다.
2. 그 slot보다 앞쪽 scan prefix에서 AABB는 겹치지만 mask는 안 겹치는 후보를 최대 K개 검사한다.
3. 더 이른 time 또는 더 좋은 cheap score의 mask slot이 있으면 채택한다.
4. 없으면 baseline AABB slot을 그대로 쓴다.
```

초기 K:

```text
K = 2 or 4
```

적용 조건:

- `SOLVER_MASK_SEARCH=1`
- dense/tardy bay에서만 켜는 variant도 별도로 둔다.

의도:

- AABB가 너무 보수적으로 막은 interlocking placement를 search scoring 단계에서 복구한다.
- full mask scan보다 비용을 제한한다.

계산 비용:

- mask overlap check가 추가된다.
- K로 상한을 둔다.
- dense geometry에서 비용이 커질 수 있으므로 wall/eval을 따로 기록한다.

위험:

- 더 이른 tighter slot이 뒤 block에게는 나쁠 수 있다.
- 이미 final build에 mask best-of가 있으므로, search scoring에 넣을 때 proxy landscape가 달라진다.

### V5. Combined Low-Risk Variant

초기 default 후보는 무겁지 않은 조합으로 제한한다.

```text
V5 = V1 + V2
```

V1/V2가 fixed-assignment 실험에서 유의미하게 이긴 뒤에만 다음 조합을 본다.

```text
V6 = V1 + V2 + V3(K=3)
V7 = V1 + V2 + V4(K=2)
```

## 4. Experiment Stages

### Stage A. Fixed Assignment, Per-Bay Materialization Test

목적:

- outer search variance를 제거한다.
- 같은 assignment에서 `solve_bay()` variant만 바꿨을 때 `Z1`이 좋아지는지 본다.

대상 assignment:

1. `a_pref`
2. `a_balanced_load`
3. `a_pref_capped`
4. default solver가 짧은 budget에서 얻은 incumbent
5. default solver가 긴 budget에서 얻은 incumbent, 가능하면

측정:

```text
for each problem:
    for each assignment source:
        for each bay:
            ids = blocks assigned to bay
            T_base = extract_tardiness(solve_bay_base(ids))
            T_var  = extract_tardiness(solve_bay_variant(ids))
```

기록 metric:

- `sum_T_base`
- `sum_T_variant`
- `delta_T`
- bay별 win/loss/tie count
- worst bay regression
- wall time per `solve_bay`
- average blocks per bay
- dense/tardy bay 여부

채택 기준:

- aggregate `sum_T` 개선이 있어야 한다.
- loss bay 수가 win bay 수보다 훨씬 많으면 탈락.
- worst regression이 큰 variant는 final-build-only candidate로 격하한다.

중요:

- 이 단계에서는 `Z2/Z3`는 고정이다. 따라서 순수 placement quality만 본다.
- 여기서 지면 full search에서 이길 가능성은 낮다.

### Stage B. Fixed Assignment, Full Objective Materialization Test

목적:

- `Z1`이 좋아진 variant가 final objective 기준으로도 유리한지 확인한다.
- assignment가 고정되어 있으므로 `Z2/Z3`는 동일하고 objective 차이는 `w1 * delta_Z1`만 반영된다.

측정:

- `_score_and_pack()`와 동일한 final materialization basis로 score한다.
- 가능하면 variant pack을 실제 solution으로 emit하고 `check_feasibility()`를 통과시킨다.

채택 기준:

- feasibility 20/20.
- aggregate objective 개선.
- hard tardy instances에서 명확한 개선 또는 neutral.

### Stage C. Search Scoring A/B, Deterministic Eval Mode

목적:

- variant를 `eval_obj1()`의 `solve_bay()` scoring path에 넣었을 때 search landscape가 좋아지는지 본다.
- wall-clock variance를 제거한다.

설정:

```text
SOLVER_MAX_EVALS=E
one instance per process
same random seed
same enabled features except tested variant
```

추천 E:

```text
E = 600   smoke
E = 1500  medium
E = 3000  confirmation
```

측정:

- final objective
- `obj1/obj2/obj3`
- eval count
- wall time
- feasibility
- incumbent trace, 가능하면

채택 기준:

- aggregate objective 개선.
- `obj1` 개선이 `obj2/obj3` 악화로 상쇄되지 않아야 한다.
- worst regression이 허용 범위 안이어야 한다.

권장 허용 범위 초안:

```text
aggregate <= -2.0%
worst regression <= +5.0%
regressed instances <= 25%
feasible = 20/20
```

초기에는 기준을 느슨하게 두되, default 전환은 더 엄격하게 한다.

### Stage D. Wall-Clock Full Solver A/B

목적:

- 실제 submission mode에서 time budget interaction을 확인한다.

설정:

```text
T = 60
T = 120
T = 180
```

반드시 비교할 것:

- 같은 time limit
- 같은 process model
- 같은 env gates
- default vs variant만 변경

측정:

- objective
- `obj1/obj2/obj3`
- wall time
- eval count, 가능하면
- final build time
- recombination time
- feasibility

분석:

- eval-mode에서는 이겼는데 wall-mode에서 졌다면 속도/예산 문제일 가능성이 높다.
- eval-mode에서도 졌다면 logic regression이다.
- wall-mode에서만 이겼다면 speed side-effect일 수 있으므로 품질 개선으로 과대해석하지 않는다.

### Stage E. Final-Build-Only Safety Test

목적:

- search scoring에 넣으면 landscape drift가 생기는 variant라도 final materialization에서는 도움이 될 수 있다.

방식:

```text
search = baseline
final build = best-of(baseline pack, variant pack)
```

이건 병렬 search가 아니다. 같은 assignment에 대해 final packing만 순차적으로 두 번 만들어 낮은 tardiness를 선택한다.

장점:

- assignment search landscape를 흔들지 않는다.
- bay-level best-of라 final packing regression을 막을 수 있다.

단점:

- final build 시간이 늘어난다.
- search scoring과 final materialization 사이 proxy drift가 남는다.

채택 기준:

- final build reserve 안에서 끝날 것.
- aggregate objective 개선.
- time limit 초과 없음.

## 5. Required Instrumentation

실험을 깔끔하게 하려면 최소한 다음 env gate를 추가한다.

```text
SOLVER_PACK_ORDER=baseline|slack_area|...
SOLVER_ORIENT_ORDER=baseline|bbox|bbox_aspect|...
SOLVER_SLOT_SCORE=0|1
SOLVER_SLOT_K=3
SOLVER_MASK_PREFIX=0|1
SOLVER_MASK_PREFIX_K=2
```

권장 trace fields:

```text
problem
variant
assignment_source
bay
n_blocks
T
wall_s
pack_mode
mask_calls
candidate_count
chosen_order_hash
```

Required cache key for experimental harnesses:

```text
cache_key = (solve_bay_method, bay, block_set)
```

`solve_bay_method` can be a readable name such as `baseline`, `slack_area`, `slack_orient`, or a compact experimental label such as `A`, `B`, `C`, `1`, `2`, `3`.

Reason:

- The same `(bay, block_set)` can have different tardiness under different placement rules.
- Mixed-oracle experiments must not reuse a tardiness value computed by another `solve_bay` method.
- Analysis needs to know which method produced each improvement/regression.

이미 있는 deterministic eval mode를 반드시 활용한다.

```text
SOLVER_MAX_EVALS=...
```

주의:

- module-level geometry cache는 `framework_solve()`에서 clear되지만, 별도 실험 harness에서도 one instance per process 또는 explicit clear를 지켜야 한다.
- wall-clock A/B는 단일 run으로 결론 내리지 않는다.

## 6. Metrics

Primary:

- full objective
- `obj1` total tardiness
- feasibility count

Secondary:

- `obj2` imbalance
- `obj3` preference penalty
- wall time
- eval count
- final build time
- solve_bay calls/sec

Placement-specific:

- per-bay tardiness delta
- number of improved/worsened/tied bays
- maximum per-bay regression
- changed entry time count
- changed orientation count
- changed placement count

Risk metrics:

- instances with objective regression
- worst objective regression
- instances where `obj1` improves but total worsens
- instances where `obj1` worsens but total improves, proxy warning

## 7. Analysis Slices

전체 평균 하나로 판단하지 않는다. 최소한 다음 slice를 본다.

By problem type:

- high tardiness / hard packing
- low tardiness / preference-dominant
- dense geometry
- many blocks
- many bays

By bay:

- bay area small vs large
- high assigned count
- high utilization
- existing baseline tardiness > 0
- baseline tardiness = 0

By effect source:

- order changed only
- orientation changed only
- slot changed only
- mask-prefix changed only

## 8. Decision Rules

Default adoption requires all of:

```text
feasible = all tested instances
eval-mode aggregate objective improves
wall-mode aggregate objective improves
worst regression is acceptable
logic regression is understood
```

If aggregate improves but regression is large:

- keep as env-gated.
- consider final-build-only best-of.
- consider instance-adaptive trigger.

If fixed-assignment improves but full search regresses:

- do not use in search scoring.
- test final-build-only.
- inspect whether `Z2/Z3` worsened due to changed assignment landscape.

If full search improves only because wall time/eval count changed:

- classify as speed side-effect, not placement-quality improvement.
- require eval-mode confirmation before adoption.

## 9. Recommended Execution Order

1. Implement V1 behind `SOLVER_PACK_ORDER=slack_area`.
2. Run Stage A on seed assignments.
3. If V1 wins, run Stage C with `E=600`, then `E=1500`.
4. Implement V2 behind `SOLVER_ORIENT_ORDER=bbox_aspect`.
5. Test V2 alone, then V1+V2.
6. Only if V1+V2 is stable, test V3 K=3.
7. Test V4 separately on hard/dense/tardy instances.
8. For any mixed result, try final-build-only bay-level best-of before search scoring adoption.

Initial priority:

```text
P1: V1 slack+geometry block order
P2: V2 orientation order
P3: V4 mask-prefix rescue
P4: V3 tiny slot scoring
```

Rationale:

- V1/V2 are almost free and directly address arbitrary greedy order.
- V4 targets known AABB/mask proxy drift.
- V3 is conceptually attractive but can be more expensive and score-sensitive.

## 10. Expected Failure Modes

### Greedy Future Damage

A placement that is locally compact can block a later urgent block.

Signal:

- fixed assignment has some large per-bay regressions.
- changed placement count is high even when `T` does not improve.

Mitigation:

- keep variant final-build-only with best-of.
- lower K.
- add "do not disturb baseline if T_base=0" policy.

### Search Landscape Drift

Better per-bay `Z1` proxy can make search choose assignments with worse `Z2/Z3`.

Signal:

- `obj1` improves but total objective worsens.
- `obj2` or `obj3` increases sharply.

Mitigation:

- final true-objective guard.
- use variant only in final build.
- add Z2/Z3 tie-break in search, separately.

### Mask-Prefix Over-Permissiveness in Greedy

Mask allows tighter placement, but tighter is not always better.

Signal:

- V4 improves tardy hard cases but regresses low-tardy cases.

Mitigation:

- enable only when baseline per-bay tardiness > 0.
- bay-level best-of.
- K=2 rather than K=4.

### Time-Neutral Assumption Fails

Even small extra scoring reduces evaluations enough to hurt wall-clock results.

Signal:

- eval-mode improves, wall-mode regresses.

Mitigation:

- restrict to final build.
- optimize implementation.
- conditionally trigger only on hard/tardy bays.

## 11. Reporting Template

각 실험 기록은 다음 형식으로 남긴다.

```markdown
## YYYY-MM-DD Variant Name

Hypothesis:
- ...

Code:
- env gate:
- commit/hash:

Setup:
- instances:
- T or E:
- default env:

Results:
| inst | base | var | delta% | Z1 base | Z1 var | Z2 delta | Z3 delta | wall base | wall var |

Placement-only:
| inst | assignment source | T base | T var | bay wins | bay losses | worst bay regression |

Conclusion:
- Adopt / reject / env-gate / final-build-only.

Notes:
- regression explanation
- next experiment
```

## 12. First Concrete Experiment

가장 먼저 할 실험은 V1이다.

Implementation sketch:

```python
def _block_order_key(prob, bay_idx, block_id):
    b = prob["blocks"][block_id]
    latest_start = b["due_date"] - b["processing_time"]
    area = _best_bbox_area_for_bay(b, prob["bays"][bay_idx])
    return (
        latest_start,
        b["due_date"],
        b["release_time"],
        -area,
        -b["workload"],
    )
```

Experiment:

```text
baseline order vs slack_area order
Stage A first
then Stage C E=600 on 20 train instances
```

Adoption threshold for first pass:

```text
Stage A aggregate sum_T improves
Stage C aggregate objective improves
no feasibility regression
worst full objective regression < +10% in exploratory run
```

If V1 passes, combine with V2. If V1 fails, do not proceed to more complex slot scoring until the failure mode is understood.

## 13. Initial Stage A Result, 2026-06-17

Implemented a standalone harness:

```text
tools/placement_experiment.py
```

The harness does not modify `submission/solver.py`, `submission/myalgorithm.py`, or any existing run script. It imports the current solver and compares experimental `solve_bay` variants on fixed assignments.

Run:

```text
python tools/placement_experiment.py --out-dir .claude/scratch/placement_experiment/full_20260617_001745
```

Scope:

```text
problems    = train/prob_1.json ... train/prob_20.json
assignments = pref, balanced, capped
variants    = baseline, slack_area, orient_bbox, slack_orient
mode        = fixed assignment materialization
```

Environment note:

```text
Stage A command used system python:
  shapely=True, ortools=True, numba=False

Local submission-like runner:
  .venv\Scripts\python.exe
  shapely=True, ortools=True, numba=True
```

Therefore this run is reliable as a placement-quality test because the numba path is intended to be behavior-invariant. Wall-time from this Stage A run must not be interpreted as final submission speed.

Aggregate result over 60 fixed-assignment cases:

| variant | objective delta | wins/losses/ties | Z1 delta |
|---|---:|---:|---:|
| `orient_bbox` | -2.99% | 35 / 15 / 10 | -500 |
| `slack_area` | -12.64% | 46 / 10 / 4 | -1681 |
| `slack_orient` | -15.34% | 47 / 9 / 4 | -1771 |

Interpretation:

- `slack_area` is the cleanest low-cost signal: large aggregate gain, but still has meaningful regressions on `prob_6`, `prob_15`, and `prob_7`.
- `orient_bbox` alone is weaker and more volatile.
- `slack_orient` gives the largest aggregate gain, but its worst regression is larger, especially `prob_16`.
- The result strongly supports continuing to Stage C, but does not justify direct default adoption yet.

Per-problem aggregate highlights for `slack_orient`:

```text
largest wins:
  prob_1  -69.10%
  prob_9  -38.62%
  prob_4  -35.35%
  prob_11 -31.34%
  prob_18 -23.73%
  prob_20 -19.33%

regressions:
  prob_16 +23.34%
  prob_13  +2.83%
  prob_6   +0.82%
```

Targeted feasibility validation:

```text
python tools/placement_experiment.py \
  --problems prob_1 prob_6 prob_16 prob_20 \
  --assignments pref balanced capped \
  --variants baseline slack_orient \
  --validate \
  --out-dir .claude/scratch/placement_experiment/validate_20260617_001746
```

Result:

```text
baseline     12/12 feasible
slack_orient 12/12 feasible
```

Next recommended gate:

```text
Stage C deterministic search-scoring A/B:
  runner = .venv\Scripts\python.exe
  baseline vs slack_area vs slack_orient
  SOLVER_MAX_EVALS=600 first, then 1500
  start with no direct default adoption

Stage D wall-clock full solver A/B:
  runner = .venv\Scripts\python.exe
  numba must be ON, matching submission-like behavior
```

## 14. Sequential Mixed-Oracle Search Result, 2026-06-17

Question tested:

> If the search previously spent 300 move/objective evaluations with the existing `solve_bay`, what happens if the same 300 evaluations are split across multiple `solve_bay` variants?

Implemented standalone harness:

```text
tools/mixed_oracle_search_experiment.py
```

This script does not modify `submission/solver.py`, `submission/myalgorithm.py`, or existing run scripts.

Runner:

```text
.venv\Scripts\python.exe
SOLVER_NUMBA=1
SOLVER_MASK_SEARCH=1
```

The run header confirmed:

```text
numba=True
```

Schedules:

| schedule | phases |
|---|---|
| `base300` | baseline 300 |
| `slack_area300` | slack_area 300 |
| `slack_orient300` | slack_orient 300 |
| `mix_b_s_so` | baseline 100 -> slack_area 100 -> slack_orient 100 |
| `mix_s_so_b` | slack_area 100 -> slack_orient 100 -> baseline 100 |

Important scoring rule:

- Each phase uses its active oracle only for local acceptance.
- Final performance is judged by the same current final materializer, `solver._score_and_pack`, to avoid comparing incompatible oracle scores.
- Each schedule used 300 phase evaluations on every instance.
- All emitted final solutions were feasible: 100 / 100.

Run:

```text
.venv\Scripts\python.exe tools\mixed_oracle_search_experiment.py \
  --out-dir .claude\scratch\mixed_oracle_search\full_20260617_081121
```

Rerun after making the cache key explicit:

```text
.venv\Scripts\python.exe tools\mixed_oracle_search_experiment.py \
  --out-dir .claude\scratch\mixed_oracle_search\methodkey_full_20260617_085146
```

The result JSON records:

```text
env.python = .venv\Scripts\python.exe
env.numba_on = True
phase.cache_key = solve_bay_method,bay,block_set
```

The aggregate numbers below are from the method-key rerun.

Aggregate vs `base300` over all 20 train instances:

| schedule | objective delta | wins/losses/ties | delta Z1 | avg wall | base avg wall |
|---|---:|---:|---:|---:|---:|
| `mix_b_s_so` | +14.07% | 0 / 19 / 1 | +170 | 5.73s | 5.54s |
| `mix_s_so_b` | +3.03% | 4 / 14 / 2 | +37 | 5.64s | 5.54s |
| `slack_area300` | +11.55% | 4 / 15 / 1 | +170 | 6.39s | 5.54s |
| `slack_orient300` | +14.22% | 1 / 18 / 1 | +175 | 5.94s | 5.54s |

Read:

- Splitting the 300 evals across placement oracles did not help under the current common final materializer.
- `mix_s_so_b` was the least bad mixed schedule and did win on `prob_4`, `prob_6`, `prob_7`, and `prob_14`, but aggregate was still worse by +3.03%.
- Starting with baseline then switching away (`mix_b_s_so`) was consistently bad: 0 wins, 19 losses, 1 tie.
- Pure slack-oriented search was much worse as a search oracle despite being good in fixed-assignment materialization.
- Average wall time was not better. `base300` averaged 5.59s; mixed schedules averaged 5.75s and 5.83s.

Interpretation:

The Stage A result says the new placement rules can materialize a fixed assignment better. This Stage C-style result says they are poor standalone search oracles under the existing move operators and final scorer. They distort the assignment landscape: the search moves toward assignments that look good under the variant oracle but materialize worse under the common final builder.

Current conclusion:

```text
Do not adopt mixed solve_bay oracle scheduling as a default search strategy.
Keep slack_area/slack_orient as candidates for:
  1. final-build-only best-of materialization, or
  2. archive/source diversification with final true-score guard.
```

Potential follow-up:

- Test `search = baseline`, `final build = best-of(baseline, slack_area, slack_orient)` sequentially.
- Test mixed-oracle only as a candidate generator whose assignments are archived, then select by true final score rather than accepting phase-local oracle improvements.
