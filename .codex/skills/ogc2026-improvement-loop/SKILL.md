---
name: ogc2026-improvement-loop
description: OGC2026 block-stowage solver improvement workflow ported from .claude agents. Use when Codex must summarize eval runs, compare OGC2026 benchmark results, propose solver hypotheses, implement one selected hypothesis, or run an approval gate for hermes/myalgorithm, athena, or future OGC2026 algorithms. Trigger examples: "지난 run 정리", "가설 제안", "H-NNN 구현", "run N과 M 비교", "승인해도 돼?", "benchmark 분석".
---

# OGC2026 Improvement Loop

이 skill은 `.claude/agents`의 4개 sub-agent를 Codex용 workflow로 이식한 것이다.
Codex에는 repo-local Claude식 agent router가 없으므로, 요청 유형에 맞는 reference를
읽고 같은 규칙을 직접 수행한다.

## Workflow Router

- Eval 결과 요약, 회귀 분석, "지난 run 어땠어?" 요청:
  [eval-analyst.md](references/eval-analyst.md)를 읽는다.
- Eval report 이후 "다음 가설", "뭘 바꿀까?", "propose hypotheses" 요청:
  [improvement-strategist.md](references/improvement-strategist.md)를 읽는다.
- 사용자가 선택한 단일 hypothesis JSON을 구현하라는 요청:
  [solver-developer.md](references/solver-developer.md)를 읽는다.
- 구현 후 post-change eval이 있고 "승인", "merge?", "run N vs M 비교" 요청:
  [approval-gate.md](references/approval-gate.md)를 읽는다.

한 요청이 여러 단계를 포함하면 순서대로 수행한다. 예를 들어 "run 정리하고 다음 가설
제안"은 eval-analyst 다음 improvement-strategist다. 구현과 gate를 섞지 말고, gate는
반드시 post-change eval이 끝난 뒤 실행한다.

## Common Rules

- 사용자에게 보내는 채팅 응답은 한국어로 작성한다. 기술용어, 파일명, event 이름,
  JSON 키, 명령어는 영어/원문을 유지한다.
- 사용자가 algo를 명시하지 않고 "벤치마크"를 말하면 기본은 Athena다:
  `py -3.12 tools/parallel_eval.py --algo athena --timelimit 60 --pattern "*.json"`.
- A/B 비교는 같은 `algo`, 같은 runner, 같은 `--workers`/`--cores-per-worker`, 같은
  instance set에서만 한다. Athena parallel run과 Hermes serial run의 objective를
  직접 비교하지 않는다.
- `tools/eval_runner.py`의 `ALGOS` dict를 algo mapping의 single source of truth로
  사용한다. 기억으로 파일/DB label을 단정하지 않는다.
- `baseline/utils.py`와 `alg_tester/utils.py`는 official scorer로 취급한다. 수정하지
  않는다.
- solver entrypoint signature `algorithm(prob_info, timelimit) -> dict`를 유지한다.
- `baseline/myalgorithm.py` 또는 `baseline/baseline_greedy.py` 변경이 `ALGORITHM.md`
  §13 trigger checklist에 해당하면 같은 작업에서 문서를 갱신한다. Athena 변경은
  `MY_NEW_ALGORITHM_EXPLANATION.md`의 대응 섹션을 갱신한다.
- `.claude/scratch/*.jsonl`을 shared append-only memory로 사용한다. 별도
  `.codex/scratch`를 만들지 않는다.
- Windows PowerShell에서 Python stdin은 `@' ... '@ | py -3.12 -` 또는
  `py -3.12 -c "..."`를 사용한다. Bash heredoc을 쓰지 않는다.
- 한글 markdown이 PowerShell 출력에서 깨져 보여도 파일 손상으로 단정하지 않는다.
  필요하면 Python `read_text(encoding="utf-8")`, `s.count("\ufffd")`,
  `unicode_escape`로 확인한다.

## Codex Execution Notes

- 이 skill은 Claude sub-agent를 자동 실행하지 않는다. Codex가 직접 workflow를
  수행한다.
- 사용자가 명시적으로 sub-agent/parallel agent work를 요청한 경우에만 Codex의
  sub-agent 도구를 사용한다. 그때도 각 agent에게 이 skill/reference의 해당 절차를
  따르라고 지시한다.
- 광역 로그/파일 읽기를 피하고 digest-first 원칙을 따른다. DB aggregate와 요약
  스크립트로 좁힌 뒤 필요한 event/worker 로그만 읽는다.
- 기존 dirty tree가 있을 수 있다. 내가 수정한 경로만 path-scoped diff로 확인하고,
  사용자 변경을 revert하지 않는다.
