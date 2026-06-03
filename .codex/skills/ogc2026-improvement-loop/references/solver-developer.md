# Solver Developer Workflow

선택된 hypothesis JSON 하나를 최소 변경으로 구현한다. 한 번에 하나의 hypothesis,
하나의 algo만 다룬다.

## Inputs

필수 입력:

- `hypothesis_id`
- `algo`: 없으면 legacy로 `myalgorithm`으로 간주하되, `target_locus`가 다른 solver를
  가리키면 사용자에게 확인한다.
- `thesis`
- `target_locus`
- `expected_impact`
- `verification_kpis`

여러 hypothesis가 주어지면 어느 하나를 구현할지 사용자에게 묻는다.

## Target Mapping

`tools/eval_runner.py`의 `ALGOS` dict가 single source of truth다.

| algo | edit target | doc sync target | verification runner |
|---|---|---|---|
| `hermes` / `myalgorithm` | `baseline/myalgorithm.py` | `ALGORITHM.md` | `tools/eval_runner.py --algo <algo>` |
| `athena` | `baseline/my_new_algorithm.py` + `baseline/athena/` | `MY_NEW_ALGORITHM_EXPLANATION.md` | `tools/parallel_eval.py --algo athena` |
| future algo | `ALGOS`의 module | 전용 doc 또는 source docstring | 설계에 맞는 runner |

## Hard Rules

1. `baseline/utils.py`와 `alg_tester/utils.py`를 수정하지 않는다.
2. 대상 algo의 `algorithm(prob_info, timelimit) -> dict` signature를 유지한다.
3. 대상 algo 파일/패키지만 편집한다. 한 hypothesis에서 여러 solver를 건드리지 않는다.
4. 대상 algo가 monkey-patch를 쓰면 모든 종료 경로에서 복구한다. Hermes는
   `utils.check_*` patch 복구가 필요하다. Athena는 monkey-patch를 쓰지 않는다.
5. `_close_event_log()` 같은 event log cleanup이 있는 algo는 종료 경로에서 유지한다.
6. 기존 사람용 `print(...)`를 보존한다. event emit을 추가하더라도 대체하지 않는다.
7. 새 top-level dependency를 추가하지 않는다. 필요하면 사용자에게 묻는다.
8. Diff가 대략 80줄을 넘을 것 같으면 스코프를 재확인한다.
9. 기존 파일은 rewrite보다 targeted edit을 우선한다.
10. 변경이 pipeline, cache, monkey-patch surface, heuristic/portfolio, init/fallback,
    SA setup/move set, budget formula, event schema, deadline propagation, known
    limitation을 건드리면 reference doc을 같은 작업에서 갱신한다.

## Procedure

1. `algo`와 `target_locus`로 대상 파일/패키지, doc, runner를 확정한다.
2. `rg -n`으로 target symbol을 찾고 현재 코드 window만 읽는다. 코드가 drift했으면
   보고하고 멈춘다.
3. 필요한 경우 `AGENTS.md`, 대상 reference doc의 관련 섹션만 읽는다.
4. 가장 작은 파일/라인 집합을 편집한다. Off-locus 편집이 있으면 이유를 기록한다.
5. 편집된 Python 파일마다 syntax check를 실행한다.
6. 삭제/rename한 symbol은 stale reference가 없는지 `rg`로 확인한다.
7. Doc sync 필요 여부를 판단하고, 필요하면 `ALGORITHM.md` 또는
   `MY_NEW_ALGORITHM_EXPLANATION.md`를 갱신한다.
8. Full eval은 실행하지 않는다. 사용자가 명시한 targeted syntax/probe만 수행한다.
9. `.claude/scratch/implemented.jsonl`에 append한다.

Syntax check 예:

```powershell
py -3.12 -c "import ast, pathlib; ast.parse(pathlib.Path('<edited_file>').read_text(encoding='utf-8')); print('OK')"
```

## Output Format

```markdown
## Change summary - H-NNN (algo: <hermes|athena|...>)

**Hypothesis**: <thesis>

**Files touched**:
- `path/to/file.py`: <변경 한 줄 설명>

**Lines added / removed**: +X / -Y

**Off-locus edits**: <있다면 이유>

**Doc sync**: <updated ... | no change needed>

**Risks / open questions**:
- <항목>

**Verification command**:
```powershell
<hypothesis verification_pattern 또는 바로잡은 runner 명령>
```

**Expected KPI movement**:
- <kpi>: <기대 방향>
```

## After-output Memory

구현 후 `.claude/scratch/implemented.jsonl`에 한 줄 JSON을 append한다.

```json
{"hypothesis_id":"H-NNN","algo":"<hermes|athena|...>","implemented_at":"<ISO timestamp>","files":{"<path>":[added,removed]},"git_sha_before":"<sha or null>"}
```

가능하면 편집 전 git sha를 캡처한다. 실패하면 `null`로 둔다.
