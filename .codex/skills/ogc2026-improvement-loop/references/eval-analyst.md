# Eval Analyst Workflow

평가 데이터를 읽어 구조화된 Markdown 보고서를 만든다. 가설이나 코드 변경은 제안하지
않는다.

## 대상 확정

1. Target run: 사용자가 명시하지 않으면 DB의 최신 `run_id`.
2. Algo DB label: `instance_results.algo`와 `events.algo`로 확정한다.
   `hermes`/`myalgorithm`은 `myalgorithm`, `athena`는 `athena`.
3. Runner: `runs.note`에 `[parallel workers=N cpw=M ...]`가 있으면 parallel.
   Athena가 serial로 돌았으면 headline/context에 benchmark 규약 위반으로 표시한다.
4. Scope: `training_set/prob_1.json`..`prob_20.json` full regression과 targeted
   probe를 섞지 않는다.

Baseline pool은 같은 algo, 같은 runner config, 같은 instance set의 최근 run만 사용한다.
다른 algo objective는 직접 비교하지 않는다.

## 데이터 소스

- `tools/ogc2026_runs.db`: `runs`, `instance_results`, `events`.
- `tools/eval_summary.py --target-run <id> --baseline-window 3`: 빠른 비교 출발점.
- `tools/event_logs/run_<id>/<instance>.jsonl`: 필요할 때만 raw event 확인.
- `tools/event_logs/run_<id>/<instance>.jsonl.worker<k>`: Athena parallel SA worker
  digest용.

SQLite schema:

```sql
runs(run_id, started_at, git_sha, git_dirty, timelimit, pattern, hostname, python_version, note)
instance_results(run_id, instance, algo, feasible, stage,
                 obj1, obj2, obj3, total_obj, wall_time,
                 sa_iterations, sa_improvements,
                 init_heuristic, init_objective, fallback_triggered, error)
events(run_id, instance, algo, t, event, payload)
```

## Digest Rules

- Raw JSONL 전체를 먼저 열지 않는다. DB aggregate와 `eval_summary.py`로 이상치
  instance를 좁힌다.
- Hermes는 `init.heuristic_result`, `init.fallback*`, `sa.complete` 중심으로 본다.
- Athena의 `sa_iterations`, `init_heuristic`, `fallback_triggered` 컬럼은 Hermes event
  parser 기준이라 대부분 비어 있다. Athena는 `athena.init.*`,
  `athena.parallel_sa.*`, worker 로그의 `sa.complete`를 본다.
- Athena worker 로그는 Python으로 `sa.temperature.init`, `sa.improvement`,
  `sa.complete`, `sa.worker.done`만 compact 파싱해 profile, large_mode,
  best_objective, iterations, improvements, winner 여부를 표로 압축한다.
- 원인 주장은 event 이름이나 DB 컬럼 수치로 근거를 달아야 한다.
- 광역 `rg ... tools/event_logs`로 시작하지 않는다.

## Procedure

1. Target run, algo, runner, instance set을 확정한다.
2. Comparable baseline을 고른다. `eval_summary.py`의 baseline window가 targeted probe를
   섞으면 DB에서 다시 고른다.
3. `eval_summary.py`를 실행하고 표를 출발점으로 삼는다.
4. 회귀, feasibility loss, fallback/all_forced 이상치만 event digest로 drill down한다.
5. 2개 이상 instance에서 반복되는 패턴만 "Patterns"에 쓴다.

## Output Format

다음 섹션을 가진 단일 Markdown 보고서를 낸다. 표를 제외하고 400 단어 이하를 목표로
한다.

```markdown
# Eval Report - Run #<id>

## Headline
<한 문장: 순방향 요약>

## Run context
- run_id, algo(DB label), runner(serial/parallel + workers/cpw), git sha 앞 8자,
  dirty 여부, timelimit, pattern, note
- # instances, # feasible, # feasibility lost, # worst-init 경로
- Athena serial 실행이면 benchmark 규약 위반 flag

## Per-instance table
instance | feasible | obj(target) | best baseline | Delta% | SA iters/imp | init | fb

## Anomalies and likely proximate causes
이상치마다 1-3문장. event 이름과 수치를 인용한다. fix 제안 금지.

## Patterns
2개 이상 instance에서 보이는 패턴과 근거 instance.

## Signals worth investigating
improvement-strategist가 볼 만한 사실 질문. "해야 한다"가 아니라 관찰/질문으로 작성.
```

## Useful Commands

PowerShell에서는 Bash heredoc 대신 `py -3.12 -c` 또는 here-string을 사용한다.

```powershell
py -3.12 tools/eval_summary.py --target-run <id> --baseline-window 3
py -3.12 -c "import sqlite3; con=sqlite3.connect('tools/ogc2026_runs.db'); print(con.execute('SELECT MAX(run_id) FROM runs').fetchone()[0])"
py -3.12 -c "import sqlite3; con=sqlite3.connect('tools/ogc2026_runs.db'); print(con.execute(\"SELECT DISTINCT algo FROM instance_results WHERE run_id=?\", (<id>,)).fetchall())"
```
