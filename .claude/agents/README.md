# OGC2026 Improvement Loop — Agent Workflow

`tools/eval_runner.py`와 `tools/eval_summary.py` 위에서 측정 가능한 개선 loop을
이루는 4개의 프로젝트 sub-agent. 각 agent는 의도적으로 좁은 책임만 가지며,
orchestration은 사용자(또는 부모 Claude 세션)가 agent 사이에 구조화된 산출물을
넘기며 수행한다.

## The loop

```
        ┌──────────────────────────────┐
        │  tools/eval_runner.py        │  SQLite + JSONL 채움
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  eval-analyst (haiku)        │  데이터 → 구조화된 Markdown 보고서
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  improvement-strategist      │  보고서 + 코드 → 1–3개 가설 (JSON)
        │  (opus)                      │
        └──────────────┬───────────────┘
                       ↓
            사용자가 하나 선택
                       ↓
        ┌──────────────────────────────┐
        │  solver-developer (sonnet)   │  가설 → 최소 코드 변경
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  tools/eval_runner.py (다시) │  새 run 적재
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  approval-gate (sonnet)      │  before/after run → APPROVE / REJECT / REVIEW
        └──────────────────────────────┘
                       │
              APPROVE  ↓        REJECT → revert (git revert) → strategist로 회귀
                      다음 가설
```

## Agent roster

| Agent | Model | Tools | 읽는 것 | 만드는 것 |
|---|---|---|---|---|
| `eval-analyst` | haiku | Bash, Read, Grep, MCP sqlite read | SQLite + JSONL | Markdown eval 보고서 |
| `improvement-strategist` | opus | Read, Grep, Glob, Bash, WebSearch, WebFetch | Eval 보고서 + 코드 + 히스토리 | 가설 JSON 배열 |
| `solver-developer` | sonnet | Read, Edit, Write, Grep, Glob, Bash | 가설 JSON 하나 | 코드 변경 + Markdown 요약 |
| `approval-gate` | sonnet | Bash, Read, MCP sqlite read | SQLite의 두 run_id | APPROVE/REJECT/REVIEW 판정 |

## 호출 패턴 (라우터가 각 agent의 `description:`에서 인식)

| 사용자가 말하면... | 라우팅 대상 |
|---|---|
| "summarize run 5" / "지난 eval 어땠어?" / "run N 정리" | `eval-analyst` |
| "다음은?" / "가설 제안" / "뭘 바꿔볼까?" | `improvement-strategist` |
| "H-007 구현해줘" / "이 가설 적용" | `solver-developer` |
| "run 8 vs 7 비교" / "H-007 승인" / "머지해도 돼?" | `approval-gate` |

## Scratch directory 계약

agent들은 loop의 cross-session 메모리를 위해 산출물을 `.claude/scratch/` 아래 영속화:

| 파일 | 쓰는 주체 | 읽는 주체 | 형식 |
|---|---|---|---|
| `hypotheses_history.jsonl` | improvement-strategist | improvement-strategist (anti-pattern), approval-gate (조회) | 한 줄당 가설 JSON |
| `implemented.jsonl` | solver-developer | approval-gate | `{hypothesis_id, files, git_sha_before, implemented_at}` per line |
| `verdicts.jsonl` | approval-gate | improvement-strategist (거절 원인 추적) | `{target_run, baseline_run, decision, ...}` per line |
| `rejected.jsonl` | (사용자, REJECT 시) | improvement-strategist | `{hypothesis_id, why}` per line |

모든 scratch 파일은 append-only. 기본적으로 gitignore 대상 — 커밋 여부는 사용자
결정.

## 수동 happy-path (현재)

loop은 아직 자동 구동이 아니다. 지금은 사람이 각 단계를 trigger.
**실행 환경 / 명령어 상세는 [ENVIRONMENT.md](../../ENVIRONMENT.md) 참고.**

```powershell
# 0. (한 번) 환경 확인.
py -3.12 tools/check_env.py
# 경로 B (.codex_deps shim) 사용 시:
$env:PYTHONPATH = "C:\Users\ADMIN\Workspace\ogc2026\.codex_deps"
$env:PYTHONIOENCODING = "utf-8"

# 1. eval 실행.
py -3.12 tools/eval_runner.py --timelimit 30 --pattern "bench_*.json" --note "<context>"

# 2. eval-analyst 호출.
#    (Claude Code에서: "지난 run 정리해줘")

# 3. improvement-strategist 호출.
#    (Claude Code에서: "그 보고서 기반으로 가설 제안해줘")

# 4. 하나 선택. 그리고:
#    (Claude Code에서: "H-NNN 구현해줘")

# 5. 같은 패턴으로 eval 재실행.
py -3.12 tools/eval_runner.py --timelimit 30 --pattern "bench_*.json" --note "post H-NNN"

# 6. Gate check.
#    (Claude Code에서: "H-NNN 승인" 또는 "run <new> vs <prev> 비교")
```

conda env (경로 A)를 쓰면 `py -3.12` 대신 `python`을 쓰고 PYTHONPATH 설정은
불필요. 자세한 것은 ENVIRONMENT.md.

## 설계 제약 (agent 편집 시 위반 금지)

- **어떤 agent도 eval 데이터를 수정하지 않는다.** SQLite DB를 쓸 권한은 오직
  `tools/eval_runner.py`.
- **각 agent는 단일 책임.** 두 번째 책임을 추가하고 있는 자신을 발견하면 분리할 것.
- **기록된 가설은 immutable.** 가설이 진화하면 새 ID를 부여.
- **gate는 판단보다 규칙.** 새 규칙은 `approval-gate.md`의 rule list에 추가; 기존
  규칙 주위에 inline LLM 판단을 끼워넣지 말 것.
- **`utils.py`는 신성 불가침** — 어떤 agent도 편집을 제안하거나 적용하지 않는다.
  새 agent를 추가하면 본문에 이를 명시할 것.
