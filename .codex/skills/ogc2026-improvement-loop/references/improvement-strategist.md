# Improvement Strategist Workflow

Eval report를 1-3개의 구체적이고 검증 가능한 hypothesis JSON으로 바꾼다. 구현하지
않고 eval도 실행하지 않는다.

## 대상 알고리즘 확정

가설 생성 전에 대상 algo를 확정한다.

1. 사용자가 명시한 algo.
2. eval report나 DB의 `algo` 값.
3. 모호하면 가장 최근 run의 algo를 확인하되, 그래도 모호하면 사용자에게 한 줄로 묻는다.

Mapping은 `tools/eval_runner.py`의 `ALGOS` dict를 single source of truth로 확인한다.

| algo | solver source | reference doc | default runner | DB label |
|---|---|---|---|---|
| `hermes` / `myalgorithm` | `baseline/myalgorithm.py` | `ALGORITHM.md` | `tools/eval_runner.py --algo <algo>` | `myalgorithm` |
| `athena` | `baseline/my_new_algorithm.py` + `baseline/athena/` | `MY_NEW_ALGORITHM_EXPLANATION.md` | `tools/parallel_eval.py --algo athena` | `athena` |
| future algo | `ALGOS`의 module | docstring 또는 전용 doc | 설계에 맞는 runner | `ALGOS`의 db_label |

Athena는 `baseline_greedy.py`에 의존하지 않고 `utils.py`를 monkey-patch하지 않는다.
Hermes 전제를 Athena 가설에 적용하지 않는다.

## 읽을 입력

- 사용자가 붙여넣거나 지정한 eval-analyst report.
- `AGENTS.md`의 architecture와 physical model. Claude-only 차이가 필요할 때만
  `CLAUDE.md`.
- 대상 algo의 현재 solver source와 reference doc. 기억으로 단정하지 않는다.
- `baseline/baseline_greedy.py` header는 operations 형식 참고용.
- `.claude/scratch/hypotheses_history.jsonl`이 있으면 최근 항목과 같은 algo 항목을
  우선 확인한다. legacy `algo` 누락 항목은 `myalgorithm`으로 간주한다.

## Physical Model Assumptions

가설이 아래 중 하나라도 깨뜨릴 수 있으면 `ogc_assumption_break_risk`에 명시한다.

- `j >= k` crane descent rule.
- 5-stage feasibility check: assignment validity, entry, exit, spatial collision,
  sequential replay.
- `utils.py` 불변.
- `algorithm(prob_info, timelimit) -> dict` contract.
- 같은 timepoint에서 EXIT가 ENTRY보다 먼저.
- Hermes 계열의 monkey-patch는 `algorithm()` 반환 전에 복구. Athena에는 적용되지
  않는다.

## Hypothesis Quality Bar

- 한 번의 eval run으로 falsifiable해야 한다.
- `target_locus`는 파일보다 좁게 함수/섹션까지 지정한다.
- smoke vs bench_hard 또는 instance class별 expected direction을 쓴다.
- rollback signal을 수치/이벤트로 명시한다.
- 기대 효과가 작은 후보는 버린다. 최소한 >5% objective 이동, >2x SA iterations 변화,
  또는 fallback/all_forced 제거 중 하나를 기대할 수 있어야 한다.
- "repair 개선", "SA 튜닝", "ML 쓰자"처럼 넓거나 vibe 기반인 후보는 거절한다.
- Targeted probe 결과로 full-suite APPROVE 수준의 결론을 내지 않는다.

## Procedure

1. Eval report에서 가장 impact 큰 증상 1-2개를 고른다.
2. 대상 algo source/doc의 관련 window만 `rg`와 line window로 읽는다.
3. hypothesis history에서 같은 algo의 실패 변종을 피한다.
4. 내부 후보를 몇 개 만든 뒤 가장 강한 1-3개만 남긴다.
5. 각 후보의 `target_locus`가 현재 코드에 존재하는지 확인한다.
6. JSON 배열만 출력한다.
7. 출력한 각 객체를 `.claude/scratch/hypotheses_history.jsonl`에 한 줄 JSON으로
   append한다. 덮어쓰지 않는다.

## Required JSON Schema

최종 출력은 JSON 배열이어야 한다. 주변 산문은 생략하거나, 사용자가 맥락을 요구한 경우
짧은 머리말 한 줄만 둔다.

```json
[
  {
    "hypothesis_id": "H-NNN",
    "algo": "<hermes | myalgorithm | athena | future label>",
    "thesis": "<X가 Y를 일으키므로 Z를 하면 Y가 W만큼 줄어든다>",
    "rationale_short": "<eval report 또는 코드 근거 2-3문장>",
    "ogc_assumption_dep": ["<의존하는 가정>"],
    "ogc_assumption_break_risk": "<risk 또는 none + 설명>",
    "target_locus": "<path:function:section>",
    "expected_impact": {
      "smoke": "<neutral | +X% obj | 기타>",
      "bench_hard": "<+/-X% obj | fallback elim | SA iters +N>",
      "feasibility": "<no change | profile X risk>"
    },
    "verification_pattern": "<올바른 runner 명령>",
    "verification_kpis": ["<DB column 또는 event 조건>"],
    "rollback_signal": "<revert를 의미하는 관측>",
    "prior_attempts": ["<historical hypothesis_id>"],
    "effort": "<5 | 15 | 60>"
  }
]
```

`hypothesis_id`는 `.claude/scratch/hypotheses_history.jsonl`의 최댓값 + 1로 단조 증가시킨다.
DB 조회 조건에는 대상 algo의 DB label을 반드시 포함한다.
