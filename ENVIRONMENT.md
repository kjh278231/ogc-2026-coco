# 실행 환경 가이드

이 문서는 **OGC2026 코드를 실제로 어떻게 돌리는가**를 명시한다. 매 세션마다
"shapely가 어디에 있지?", "Python 3.12를 어떻게 부르지?"를 다시 찾지 않도록.

## TL;DR

```powershell
# 1) 환경 검증 (한 줄로 모든 것 확인)
py -3.12 tools/check_env.py

# 2) eval 실행 (가장 흔한 명령)
$env:PYTHONPATH = "C:\Users\ADMIN\Workspace\ogc2026\.codex_deps"
$env:PYTHONIOENCODING = "utf-8"
py -3.12 tools/eval_runner.py --timelimit 30 --pattern "prob_*.json" --note "<context>"

# 3) Geometry debug (실패 원인 분해)
$env:PYTHONPATH = "C:\Users\ADMIN\Workspace\ogc2026\.codex_deps"
$env:PYTHONIOENCODING = "utf-8"
py -3.12 tools/geometry_debug.py --instance training_set/prob_1.json --probe-edd
```

bash (Git Bash, WSL 등)에서는 동일한 명령:

```bash
PYTHONPATH=.codex_deps PYTHONIOENCODING=utf-8 py -3.12 tools/eval_runner.py --timelimit 30 --pattern "prob_*.json"
```

이게 안 먹히면 §4 Troubleshooting 참고.

---

## 1. Python 인터프리터

| 항목 | 값 |
|---|---|
| 요구 버전 | **Python 3.12** (정확히 3.12, 3.11도 3.13도 안 됨) |
| 이유 | `.codex_deps/`의 vendored shapely가 `cp312-win_amd64.pyd`로 빌드되어 있음 — 다른 minor 버전에서는 import 실패 |
| Windows 호출 방법 | `py -3.12 ...` (Python Launcher 권장) 또는 `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe` 직접 호출 |
| 확인 명령 | `py -3.12 --version` → `Python 3.12.x` 여야 함 |

`py` launcher는 Windows Python 표준 설치에 함께 들어온다. `py -0`로 사용
가능한 버전 목록을 확인할 수 있다.

---

## 2. 의존성 설치 — 두 가지 경로

### 경로 A — Conda env (정식, 권장 — 깨끗한 머신)

```bash
conda env create -f ogc2026_env.yml   # 한 번
conda activate ogc2026                # 매 세션
```

이렇게 만들면 모든 deps가 들어온다: shapely, numpy, pyqt6, gurobi, ortools,
torch, tensorflow, scikit-learn, numba 등. `ogc2026_env.yml` 참고.

이 경로에서는 **`PYTHONPATH=.codex_deps`를 설정하면 안 된다** — vendored
shim이 conda env의 같은 패키지와 충돌한다.

활성화된 conda env 안에서는:

```bash
python tools/eval_runner.py --timelimit 30 --pattern "prob_*.json"
```

`py -3.12` 대신 그냥 `python`.

### 경로 B — `.codex_deps/` shim (최소, 현재 머신 기본값)

conda를 설치하기 싫거나 `ogc2026` env이 없을 때. 이 repo의 `.codex_deps/`에
`shapely-2.1.2` + `numpy-2.4.6`이 cp312 wheel 형태로 벤더링되어 있다.

```powershell
# 한 번만: Python 3.12 자체가 설치돼 있어야 함
winget install Python.Python.3.12

# 매번 명령 실행할 때 PYTHONPATH 지정
$env:PYTHONPATH = "C:\Users\ADMIN\Workspace\ogc2026\.codex_deps"
$env:PYTHONIOENCODING = "utf-8"
py -3.12 tools/eval_runner.py ...
```

이 경로의 한계:
- **포함된 패키지**: shapely, numpy만. gurobi, ortools, torch 등 **무거운
  optional deps는 없음**.
- **결론**: Hermes solver (`baseline/myalgorithm.py`), 평가 인프라
  (`tools/eval_runner.py`, `tools/geometry_debug.py`), MCP server는 동작. GUI
  tester (`alg_tester_app.py`), Gurobi/Xpress 기반 비교, 강화학습 코드는 **돌지
  않음** — 그건 경로 A 필요.

---

## 3. 환경 변수

경로 B (`.codex_deps` shim)에서 필수:

| 변수 | 값 | 이유 |
|---|---|---|
| `PYTHONPATH` | 절대경로 `<repo>/.codex_deps` | shim에서 shapely/numpy 발견 |
| `PYTHONIOENCODING` | `utf-8` | Windows 콘솔 기본 `cp949`가 ASCII 외 문자(em-dash, 한글 등)에서 `UnicodeEncodeError` 일으킴 |

경로 A에서는 둘 다 **설정하지 않는다**.

별도로 평가 인프라가 사용하는 변수:

| 변수 | 값 | 누가 설정 |
|---|---|---|
| `OGC2026_EVENT_LOG` | JSONL 파일 경로 | `tools/eval_runner.py`가 인스턴스마다 자동 설정. 직접 호출 시에도 설정하면 myalgorithm이 trace 기록 |

---

## 4. Troubleshooting

흔한 에러와 대응:

| 에러 | 원인 | 해결 |
|---|---|---|
| `ModuleNotFoundError: No module named 'shapely'` | `PYTHONPATH=.codex_deps` 누락 (경로 B) 또는 conda env 미활성 (경로 A) | §2 둘 중 하나 실행 |
| `ModuleNotFoundError: No module named 'shapely.lib'` | Python 버전 mismatch — 3.12가 아닌 인터프리터로 `.codex_deps` 안의 cp312 .pyd를 import 시도 | `py -3.12`로 명령 다시 호출 |
| `'py' is not recognized` | Python Launcher 미설치 | `winget install Python.Python.3.12` 또는 절대경로 호출 |
| `UnicodeEncodeError: 'cp949' codec can't encode character '—'` | Windows 콘솔 인코딩 | `$env:PYTHONIOENCODING = "utf-8"` |
| `conda: command not found` | conda 미설치 또는 PATH 미설정 | 경로 B로 전환하거나 Anaconda 설치 |
| `mcp-server-sqlite` 미발견 | 패키지 미설치 | `py -3.12 -m pip install --user mcp-server-sqlite` |
| eval_runner가 무한 hang | 일반적으로 myalgorithm 안의 deadline 미준수 또는 부적절한 timelimit | `--timelimit`을 5초로 낮춰 재현; deadline 전파(ALGORITHM.md §11) 점검 |

---

## 5. 자주 쓰는 명령 치트시트

모두 경로 B 기준. 경로 A에서는 앞의 `$env:PYTHONPATH=...; $env:PYTHONIOENCODING=...; py -3.12` 부분을 `python`으로 바꾸면 됨.

### 환경 검증

```powershell
py -3.12 tools/check_env.py
```

### Eval 실행

```powershell
$env:PYTHONPATH = "C:\Users\ADMIN\Workspace\ogc2026\.codex_deps"
$env:PYTHONIOENCODING = "utf-8"

# training_set 전체
py -3.12 tools/eval_runner.py --timelimit 30 --pattern "prob_*.json" --note "<context>"

# 단일 training_set instance (빠른 sanity)
py -3.12 tools/eval_runner.py --timelimit 30 --pattern "prob_1.json" --note "sanity"

# 단일 instance
py -3.12 tools/eval_runner.py --timelimit 60 --pattern "prob_20.json"
```

### Eval 요약 (run N과 same-algo baseline window 비교)

`tools/eval_summary.py`는 대상 run의 `instance_results.algo`를 자동 감지하고,
`run_id < target_run`인 같은 `algo`의 과거 run만 baseline pool로 사용한다.
`--target-run`을 생략하면서 특정 solver의 최신 run을 보고 싶으면 `--algo`를
명시한다.

```powershell
# 특정 run 요약 (DB algo 자동 감지)
py -3.12 tools/eval_summary.py --target-run <run_id> --baseline-window 3

# 특정 algo의 최신 run 요약
py -3.12 tools/eval_summary.py --algo athena --baseline-window 3
```

### Geometry debug (stage 2/3/4 실패 분해)

```powershell
$env:PYTHONPATH = "C:\Users\ADMIN\Workspace\ogc2026\.codex_deps"
$env:PYTHONIOENCODING = "utf-8"

# 모드 A: 이미 만들어진 solution을 분석
py -3.12 tools/geometry_debug.py --instance training_set/prob_1.json --solution path/to/sol.json

# 모드 B: raw EDD greedy로 probe (repair 없음)
py -3.12 tools/geometry_debug.py --instance training_set/prob_1.json --probe-edd --probe-budget 10 --dump-solution tools/debug_dumps/prob_1_edd_raw.json
```

### SQLite MCP server (Claude Code가 자동 호출, 수동 디버그용)

```powershell
py -3.12 tools/run_sqlite_mcp.py --db-path tools/ogc2026_runs.db
```

Stdio MCP 프로토콜로 응답하므로 수동 호출은 디버깅 용도. 등록은
`.mcp.json`에서.

### DB 빠른 쿼리 (MCP가 아닌 Python sqlite3 모듈)

SQLite DB는 `tools/ogc2026_runs.db`이고 주요 테이블은 다음 세 개다.

- `runs`: `run_id`, `started_at`, `git_sha`, `git_dirty`, `timelimit`,
  `pattern`, `hostname`, `python_version`, `note`
- `instance_results`: `run_id`, `instance`, `algo`, `feasible`, `stage`,
  `obj1`, `obj2`, `obj3`, `total_obj`, `wall_time`, `sa_iterations`,
  `sa_improvements`, `init_heuristic`, `init_objective`,
  `fallback_triggered`, `error`
- `events`: `run_id`, `instance`, `algo`, `t`, `event`, `payload`

아래 예시는 `run_id=26`, `algo='athena'` 기준이며 값만 바꿔서 사용한다.

```powershell
# 최근 run 목록
py -3.12 -c "import sqlite3; c=sqlite3.connect('tools/ogc2026_runs.db'); [print(r) for r in c.execute('SELECT run_id, started_at, timelimit, pattern, note FROM runs ORDER BY run_id DESC LIMIT 10')]"

# run에 기록된 DB algo label 확인
py -3.12 -c "import sqlite3; c=sqlite3.connect('tools/ogc2026_runs.db'); [print(r) for r in c.execute('SELECT DISTINCT algo FROM instance_results WHERE run_id=?', (26,))]"

# 특정 run/algo의 instance별 결과
py -3.12 -c "import sqlite3; c=sqlite3.connect('tools/ogc2026_runs.db'); [print(r) for r in c.execute('SELECT instance, feasible, stage, total_obj FROM instance_results WHERE run_id=? AND algo=? ORDER BY instance', (26, 'athena'))]"
```

---

## 6. PATH / 경로 참고

| 경로 | 용도 |
|---|---|
| `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe` | Python 3.12 인터프리터 본체 |
| `C:\Users\ADMIN\AppData\Roaming\Python\Python312\site-packages\` | `pip install --user`로 들어간 패키지 (mcp-server-sqlite 등) |
| `C:\Users\ADMIN\AppData\Roaming\Python\Python312\Scripts\` | `pip install --user`의 .exe shim (PATH에 없으면 직접 호출) |
| `<repo>/.codex_deps/` | shapely + numpy vendored shim (Python 3.12 cp312 전용) |
| `<repo>/training_set/` | 기본 benchmark/training-set instance (`prob_*.json`) |
| `<repo>/tools/event_logs/run_<N>/` | eval_runner가 만든 per-instance JSONL trace |
| `<repo>/tools/ogc2026_runs.db` | SQLite 결과 저장소 |

---

## 7. 새 머신에서의 minimal 시작

처음 보는 머신에서 즉시 동작시키는 절차:

```powershell
# 1. Python 3.12 설치
winget install Python.Python.3.12

# 2. repo clone (이미 했다고 가정)

# 3. mcp-server-sqlite (선택, MCP 쓸 거면)
py -3.12 -m pip install --user mcp-server-sqlite

# 4. 검증
py -3.12 tools/check_env.py

# 5. 첫 eval
$env:PYTHONPATH = "$PWD\.codex_deps"
$env:PYTHONIOENCODING = "utf-8"
py -3.12 tools/eval_runner.py --timelimit 30 --pattern "prob_1.json" --note "first run"
```

`.codex_deps/`가 repo에 함께 들어있으므로 추가 pip install이 필요 없다.
무거운 deps (Gurobi 등)를 쓰려면 그때 가서 conda env(§2 경로 A) 셋업.
