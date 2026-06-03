# Approval Gate Workflow

Post-change eval과 baseline run을 비교해 APPROVE, REJECT, REVIEW 중 하나를 낸다.
코드 수정, 가설 제안, eval 재실행은 하지 않는다.

## Inputs

- `target_run`: 변경 후 run_id.
- `baseline_run`: 변경 전 run_id. 없으면 target보다 이전의 같은 algo, 같은 runner
  config, 같은 instance set run을 자동 선택한다.
- 선택: hypothesis JSON. `algo`와 `expected_impact` 검증에 사용한다.

비교 가능한 baseline이 없으면 reason `no prior baseline`으로 REVIEW를 낸다.

## R0 Pre-check

1. 대상 DB label 확정: hypothesis `algo`가 있으면 mapping한다. 없으면 target run의
   `instance_results.algo`를 조회한다. target run에 여러 algo가 섞이면 REVIEW.
2. Baseline과 target은 같은 algo여야 한다. 다르면 `cross-algo comparison` REVIEW.
3. Runner와 `--workers`/`--cores-per-worker`가 같아야 한다. 다르면
   `non-comparable runner config` REVIEW.
4. Instance set이 같아야 한다. `prob_*` full regression과 targeted probe를 섞으면
   REVIEW.

R1-R6의 모든 쿼리는 R0에서 정한 `db_label`로 필터한다.

## Decision Rules

### R1 hard - feasibility regression 금지

Baseline feasible instance는 target에서도 feasible이어야 한다. 위반하면 REJECT.

### R2 hard - smoke 5% 초과 회귀 금지

`smoke_` instance에서 both feasible이면 objective 회귀율이 5%를 넘으면 REJECT.

### R3a hard/soft - canonical `prob_*` full-suite

Full `prob_1`..`prob_20` 비교에만 적용한다.

- 어떤 feasible-both `prob_*` instance라도 objective가 5% 초과 회귀하면 REJECT.
- 회귀 instance 수가 개선 instance 수보다 많으면 soft fail.
- `sum(target.total_obj) / sum(baseline.total_obj) > 1.001`이면 soft fail.
- targeted probe에는 적용하지 않는다.

### R3b soft - bench net 유지

`bench_` 또는 `my_` instance의 feasible-both mean ratio가 1.005를 넘으면 soft fail.

### R4 soft - SA throughput

- Hermes/myalgorithm: `baseline.sa_iterations > 0`인 instance에서 target이 0이거나
  50% 미만이면 soft fail.
- Athena: `sa_iterations` 컬럼은 보통 비어 있으므로 N/A가 기본이다. 엄밀히 보려면
  worker 로그 `sa.complete.iterations`를 합산/평균한다. `athena.parallel_sa.done`의
  `n_feasible == 0`이면 soft fail.

### R5 hard - worst-init/fallback 증가 금지

- Hermes/myalgorithm: matched set의 `SUM(fallback_triggered)`가 증가하면 REJECT.
- Athena: events의 `athena.init.all_forced` 발생 instance 수가 증가하면 REJECT.
- Future algo: source/doc에서 guaranteed-feasible 최후 fallback event를 찾는다. 측정
  불가하면 N/A.

### R6 soft - expected impact

Hypothesis JSON이 있으면 선언된 `expected_impact`와 실제 KPI 움직임을 비교한다.
자릿수 이상으로 어긋나면 soft fail.

## Verdict Matrix

- 모든 hard pass, 모든 soft pass: APPROVE.
- hard fail 하나 이상: REJECT.
- hard pass, soft fail 하나: REVIEW, lean approve.
- hard pass, soft fail 둘 이상: REVIEW, lean reject.
- N/A는 fail count에 넣지 않는다. R0 실패는 REJECT가 아니라 REVIEW다.

## Useful Queries

PowerShell에서는 `sqlite3` CLI가 없을 수 있으므로 `py -3.12 -c`를 기본으로 쓴다.

```sql
SELECT instance, feasible, stage, total_obj, sa_iterations, sa_improvements,
       init_heuristic, fallback_triggered
FROM instance_results
WHERE run_id = <id> AND algo = '<db_label>';

SELECT b.instance, b.feasible, b.total_obj, b.sa_iterations, b.fallback_triggered,
       t.feasible, t.total_obj, t.sa_iterations, t.fallback_triggered
FROM instance_results b
JOIN instance_results t ON b.instance = t.instance AND b.algo = t.algo
WHERE b.run_id = <baseline> AND t.run_id = <target> AND b.algo = '<db_label>';

SELECT run_id, COUNT(DISTINCT instance) AS n_all_forced
FROM events
WHERE run_id IN (<baseline>, <target>) AND event = 'athena.init.all_forced'
GROUP BY run_id;
```

## Output Format

```markdown
# Verdict - Run #<target> vs #<baseline> (algo: <db_label>)

**Decision**: APPROVE | REJECT | REVIEW

**Hypothesis tested**: H-NNN - <thesis if provided>

## Rule evaluation
| Rule | Result | Detail |
|---|---|---|
| R0 algo/runner | PASS / REVIEW | <detail> |
| R1 feasibility | PASS / FAIL | <instances> |
| R2 smoke regression | PASS / FAIL | <max %, instance> |
| R3a prob full-suite | PASS / FAIL / SOFT FAIL / N/A | <aggregate, counts> |
| R3b bench mean | PASS / SOFT FAIL / N/A | <mean ratio> |
| R4 SA throughput | PASS / SOFT FAIL / N/A | <detail> |
| R5 worst-init rate | PASS / FAIL / N/A | <detail> |
| R6 expected impact | PASS / SOFT FAIL / N/A | <detail> |

## Per-instance deltas
<feasibility, objective, percent delta, SA, fallback/all_forced 표>

## Rationale
<REVIEW 또는 자명하지 않은 REJECT일 때만 2-4문장>

## Suggested next action
- APPROVE: 변경 commit. 다음 가설로.
- REJECT: Revert 여부를 사용자에게 확인하고 실패 rule을 기록.
- REVIEW: 사용자 결정이 필요한 trade-off 나열.
```

## After-output Memory

Verdict 후 `.claude/scratch/verdicts.jsonl`에 한 줄 JSON을 append한다.

```json
{"target_run":N,"baseline_run":M,"decision":"APPROVE|REJECT|REVIEW","hard_fails":[],"soft_fails":[],"hypothesis_id":"H-NNN-or-null","timestamp":"<ISO>"}
```
