---
name: geometry-debug
description: |
  Decompose an OGC2026 stage-2/3/4 feasibility failure into block-, layer-, and
  time-resolved violation records. Use when an eval run shows infeasible
  solutions (feasible=0 with stage in {2,3,4}), when myalgorithm's portfolio
  reports `init.heuristic_result feasible=false stage=2` events, or when the
  user asks "why does block N collide" / "why can't the crane enter at t=K" /
  "what's blocking exit on bay J" / "왜 stage 2가 안 풀려?" / "block N이 어디서
  막혀?" / "EDD가 왜 죽었어?". Outputs include: per-block obstruction records
  (existing-block id, layer k/j pair, descent-sweep vs final-position, overlap
  area in grid units), boundary violations, and pairwise collision records by
  bay. TRIGGER when the user wants causal explanation of a stage failure, not
  just the stage number. SKIP for stage 1 (assignment validity — the
  violations list is already self-explanatory), for stage 5 (replay — same),
  and for objective tuning (use eval-analyst / improvement-strategist).
---

# Geometry Debug Skill

당신은 **OGC2026 feasibility check의 stage 2 (crane entry), 3 (crane exit), 4 (spatial
collision) 실패 원인을 진단**하고 있다. 공식 `check_feasibility`는 stage 번호와
사람이 읽기 위한 문자열 목록만 돌려준다 — fix를 설계하기엔 부족하다. 이 skill은
`tools/geometry_debug.py`를 구동해서 실패의 원인을 분해한다.

## 언제 호출하는가

- `instance_results` row가 `feasible=0`이고 `stage IN ('2','3','4')`일 때.
- event log에 `init.heuristic_result feasible=false stage=2`가 있고, 특히
  portfolio의 *모든* seed가 같은 stage에서 실패할 때.
- 사용자가 다음 중 하나를 묻는다:
  - "왜 stage 2가 안 풀려?"
  - "block N이 어디서 막혀?"
  - "EDD가 왜 죽었어?"
  - "descent path 충돌은 어디서?"

## 언제 호출하지 않는가

- Stage 1 (assignment validity): 공식 `violations` 문자열이 이미 충분히 구체적
  (`Stage1: block 17 has entry_time < release_time`).
- Stage 5 (replay ordering): 같은 이유 — 문자열이 위반 op을 명시함.
- Feasibility는 유지되는데 objective가 회귀한 경우: `eval-analyst` 사용.
- 가설 생성: `improvement-strategist`. 이 skill은 *제안*이 아니라 *사실*만
  만든다.

## 두 가지 입력 모드

헬퍼는 solution JSON을 직접 받거나 probe로 직접 만든다.

### Mode A — 이미 만들어진 solution 분석

```bash
PYTHONPATH=.codex_deps PYTHONIOENCODING=utf-8 \
  py -3.12 tools/geometry_debug.py \
    --instance alg_tester/example/benchmark/<name>.json \
    --solution path/to/solution.json \
    --limit 25
```

`results_*.json`이나 dump된 실패 attempt처럼 이미 solution JSON이 있을 때.

### Mode B — raw EDD greedy로 probe (repair 없음)

```bash
PYTHONPATH=.codex_deps PYTHONIOENCODING=utf-8 \
  py -3.12 tools/geometry_debug.py \
    --instance alg_tester/example/benchmark/<name>.json \
    --probe-edd --probe-budget 10 \
    --dump-solution tools/debug_dumps/<name>_edd_raw.json \
    --limit 25
```

run_2/run_3 bench_B5에서 portfolio 첫 seed가 맞은 stage 2 실패를 재현할 때 사용.
`--dump-solution`은 captured raw greedy 결과를 저장해두어, 나중에 다시 돌리지 않고
Mode A로 재분석할 수 있게 한다.

## 출력 읽는 법

스크립트는 stage별로 markdown 스타일 섹션을 출력한다:

```
# Geometry Debug — instance=<name>
- bays=N, blocks=M, ENTRY ops=..., EXIT ops=...
- check_feasibility -> feasible=False, stage=2, #violations=K

## Stage 1 — Assignment validity
- <check_feasibility가 내놓은 문자열, 있다면>

## Stage 2 — Crane entry feasibility
_Total blocks with stage-2 violations: P (showing up to 25)_

### Block 105 ENTRY @ t=1 bay=0 pos=(40,3) orient=4
  - existing block 35: layers k(new)=[0,1] j(exist)=[0,1] [final=2, descent-sweep=1] max_overlap=48.93
```

해석: **block 105가 t=1에 bay 0으로 들어갈 때 block 35에 막힘**. Descent path에
*final-position* overlap(k==j, layer 두 쌍)과 *descent-sweep* overlap(j>k, 한 쌍)이
모두 있고, 최대 overlap이 ~49 grid unit — near-miss가 아닌 구조적 충돌이다.

### Stage-2 / 3 record 필드

| 필드 | 의미 |
|---|---|
| `k(new)` | 막힌 new-block layer indices |
| `j(exist)` | 막은 existing-block layer indices |
| `final=X` | `k == j` 인 (k, j) 쌍 개수 (resting position에서의 충돌) |
| `descent-sweep=Y` | `j > k` 인 쌍 개수 (existing 상위 layer가 descent 중에 sweep) |
| `max_overlap` | 모든 (k, j) 쌍의 Shapely intersection area 최댓값 |
| `BOUNDARY` (라벨) | new block이 bay polygon 밖으로 삐져나옴 |

### Stage-4 record 필드

| 필드 | 의미 |
|---|---|
| `blocks A↔B` | co-present block A, B의 pairwise overlap |
| `layer` | overlap이 일어난 layer index |
| `overlap_area` | Shapely intersection area |
| `BOUNDARY violation: block X` | X가 자기 bay 밖으로 나감 (pair 아님) |

## 해석 playbook

헬퍼를 돌린 뒤에는 각 위반을 **추정 root cause**로 분류한다 — strategist에게
넘기는 핵심 가치다. 살펴봐야 할 패턴:

1. **큰 final-position overlap 다수 (`max_overlap > 10`, `final >= 1`)**
   → `_find_earliest_slot` 또는 그 monkey-patch 변종이 여러 block을 같은 (x, y) 같은
   시간대에 placement. 깨진 건 placement scoring이지 crane logic이 아니다.

2. **작은 overlap의 descent-sweep 위주 (`max_overlap < 5`, `descent-sweep >= 1`,
   `final == 0`)** → 최종 resting position은 깨끗하지만 더 키 큰 existing block의
   상위 layer (`j > 0`)가 new block 강하를 막는다. crane geometry에 수직 clearance나
   timing 조정이 필요.

3. **한 existing block이 여러 new block을 막음** → 그 existing block이 너무 일찍 /
   중앙에 놓였다; peak load 동안 결정적 strip을 점유. 늦추거나 옮기는 걸 고려.

4. **위반이 한 timepoint에 몰림 (예: 반복적으로 `t=2`)** → 같은 창에 entry가 과밀.
   문제는 공간이 아니라 시간 분포.

5. **`BOUNDARY`인 block의 footprint가 `bay_width-1` 근처** → orient 선택이나 x offset이
   block을 삐져나오게 함. placement scorer의 orient 선택을 제한해야 함.

## 사용자에게 돌려줄 출력 형식

헬퍼를 구동한 뒤 사용자에게 다음으로 응답한다:

1. **Headline** — 한 문장: stage, 개수, 지배 패턴. 예:
   "Stage 2 on bench_B5_b120: 9 blocks fail, 7 are large final-position
   overlaps (greedy placement collision)."
2. **Top-3 violations 표** — block id, 막는 block id, area, k/j, sweep/final.
3. **패턴 분류** — 위 playbook 5개 중 어느 것에 해당하는지.
4. **다음 agent로의 포인터** — `improvement-strategist`를 향한 한 문장: "Target locus:
   monkey-patched `custom_find_earliest_slot` x/y candidate enumeration."

코드 변경은 제안하지 말 것 — `improvement-strategist`의 일이다. eval을 재실행하지
말 것. `utils.py`를 수정하지 말 것.

## Hard rules

- 항상 헬퍼 출력을 인용. block id와 overlap area는 그대로 — 숫자 없이
  "small" / "large"로 뭉뚱그리지 말 것.
- 헬퍼가 죽거나, 명백히 infeasible한 solution에 0 violation을 돌려주면 이상 신호로
  보고할 것; 덮지 말 것.
- 길이 상한: 사용자 응답은 350 단어 이하.
- 한 instance당 한 번 호출. 여러 instance를 보고 싶으면 헬퍼 호출을 한 Bash 파이프로
  묶어서 처리.
