# Hermes Solver — Algorithm Reference

이 문서는 현재 `baseline/myalgorithm.py` ("Hermes" solver)의 동작을 처음부터 끝까지
설명한다: 파이프라인 단계, 자료구조, monkey-patch, 휴리스틱 portfolio, simulated
annealing loop, 시간 예산, 그리고 `baseline/baseline_greedy.py`와의 상호작용까지.

> **Maintenance rule.** `baseline/myalgorithm.py` 또는 `baseline/baseline_greedy.py`를
> 수정할 때 아래 항목(파이프라인 순서, cache 구조, monkey-patch surface, heuristic 집합,
> SA move 분포, 예산 공식, event schema, deadline 전파) 중 하나라도 바뀌면 **같은 커밋
> 안에서** 이 문서의 해당 섹션을 업데이트해야 한다. 마지막 "Maintenance rule" 섹션의
> 트리거 체크리스트를 참고할 것.

---

## 1. 한 단락 요약

Hermes는 OGC2026 block-stowage 문제용 portfolio + simulated annealing 메타휴리스틱이다.
각 orientation별로 OBB와 per-layer Shapely polygon을 local 좌표에 미리 캐싱해두고,
`utils.check_entry / check_exit / check_collisions`을 3-stage 계층 필터(AABB → OBB →
full polygon)로 monkey-patch한 뒤, `baseline_greedy._find_earliest_slot`도 future
EXIT blocking까지 거부하는 crane-aware 변종으로 교체한다. 그다음 4개의 named priority
heuristic(EDD, SlackRatio, MST, LargestArea)을 per-seed 시간 cap 안에서 평가하여 최적의
feasible seed를 고르고, 모든 seed가 실패하면 곧장 forced-placement로 폴백한다(H-001
이후). 이후 swap/insert/invert SA loop가 permutation 공간을 90% wall-clock 예산
안에서 탐색하며, per-iteration repair cap으로 한 neighbor가 시간을 다 잡아먹지 않도록
보호한다. 모든 patch는 `algorithm()`이 반환되기 전에 복구된다. `OGC2026_EVENT_LOG`
환경변수가 설정되어 있으면 구조화된 JSONL 이벤트가 기록되어 `tools/eval_runner.py`가
(run, instance)별 trace를 수집할 수 있다.

---

## 2. 상위 파이프라인 (`algorithm(prob_info, timelimit=60)`)

| Phase | Lines | 역할 |
|---|---|---|
| 0 | 383–387 | `start_time` 기록, event log 초기화, `hard_deadline`과 `safety_margin = clamp(0.05, timelimit*0.02, 0.5)` 산출 |
| 1 | 396 | `precompute_obbs(prob_info)` — `obb_cache`와 `local_polys_cache` 채움 |
| 2 | 399–402 | `utils.check_entry/exit/collisions`와 `baseline_greedy._find_earliest_slot` monkey-patch |
| 3 | 414 | `prob_info["bays"]`에서 `Bay` 객체 생성 |
| 4 | 417–487 | 12-heuristic portfolio 정의 (평가는 4개만 하지만 sort 비용이 싸서 전부 미리 만듦) |
| 5 | 489–563 | `evaluate_permutation`과 `evaluate_forced_permutation` 클로저 정의 |
| 6 | 570–622 | Init 선택: `target_heuristics` 평가 후, 모두 infeasible이면 `forced_direct` 폴백 (H-001) |
| 7 | 626–728 | `search_deadline`까지 Simulated Annealing loop |
| 8 | 730–740 | 원본 복구, `algo.end` emit, event log 닫고 `best_sol` 반환 |

---

## 3. 자료구조와 캐시

### 3.1 `obb_cache : dict[(block_id, orient_idx) -> Shapely Polygon]`

`layers[0][0]`이 `(0, 0)`에 오도록 anchor된 모든 layer vertex 집합의 minimum
rotated rectangle. World OBB는 Shapely `translate(local, block.x, block.y)`로 얻고,
`block._world_obb`에 lazy-memoise되어 각 `Block` 인스턴스가 translate 비용을 최대 한
번만 지불한다.

### 3.2 `local_polys_cache : dict[(block_id, orient_idx) -> list[Polygon | None]]`

orientation별로 *layer마다 하나씩* Shapely polygon을 local 좌표에 저장. World poly는
per-layer `translate(p, bx, by)`로 얻고 `block._world_polys`에 memoise된다. 이 캐시
덕분에 `_place_blocks`가 SA 루프에서 매번 새로 만드는 `Block` 인스턴스마다 Shapely
polygon을 재구성하지 않아도 되어, n=120+ 인스턴스에서도 30초 안에 SA가 9–20회
반복할 수 있다.

### 3.3 Anchor convention

두 캐시 모두 orientation의 `layers[0][0]`을 anchor로 한다. 따라서 최종 `(block.x,
block.y)`는 OBB centroid나 AABB corner가 아니라 **`layers[0][0]`의 world 좌표**다.

---

## 4. Monkey-patches

모든 patch는 `algorithm()` 상단(라인 399–402)에서 설치되고 하단(라인 730–733)에서
복구된다. 원본은 *모듈 import 시점에* `original_*` 변수에 저장해서 patch가 자기 자신을
참조하지 않게 한다.

### 4.1 `custom_check_entry` / `custom_check_exit` (3-stage filter)

```
Stage 1: AABB overlap        (utils._bb_overlap, O(1))
Stage 2: OBB overlap         (Shapely intersects on cached OBBs)
Stage 3: Per-layer polygon intersection with j >= k descent rule
```

세 단계 모두 실패해야만 `EntryObstruction`이 생성된다. 앞 두 stage는 비용이 싸서
dense bay에서 대부분의 pair를 걸러낸다. 만약 block이 boundary check에 실패하면 원본
`utils.check_entry`로 폴스루되어 공식 boundary-violation sentinel이 유지된다.

매 existing block마다 `baseline_greedy._active_deadline_reached()`를 호출해 deadline이
지나면 `[None]`을 반환 — "deadline 끊김, 이 결과 믿지 마라" 신호다.

### 4.2 `custom_check_collisions`

같은 3-stage filter를 같은 bay 안의 pairwise로 적용. 공식 scorer가 기대하는 표준
`CollisionResult` 레코드를 반환한다.

### 4.3 `custom_find_earliest_slot`

`baseline_greedy._find_earliest_slot`을 대체한다. **Future EXIT Blocking Prevention**을
추가: 이미 placed된 `b_other` 중 exit time이 `[entry, exit_t)` 안에 있는 모든 block에
대해 `check_exit(bay, [new_blk], b_other, fast=True)`를 호출 — `new_blk`이
`b_other`의 exit를 막으면 그 candidate `entry`는 거부된다. "이 block을 여기에 놓으면
나중에 X가 빠져나갈 때 후회할 것"을 사전 차단하는 코드베이스 안의 유일한 장치다.

다음 조건에서도 거부:
- Stage-2 entry obstruction (`check_entry`, co-present blocks 대상)
- Stage-3 exit obstruction (`check_exit`)
- Stage-4 interior collision (`check_collisions`, `[entry, exit_t)`에 strictly 포함된
  block 대상)

`_active_deadline_reached()`를 곳곳에서 호출해 deadline을 존중한다.

---

## 5. 초기 휴리스틱 portfolio

12개 heuristic이 eager하게 계산된다(라인 427–487). 실제로 평가되는 것은
**`target_heuristics = ["EDD", "SlackRatio", "MST", "LargestArea"]`** (라인 570).

| 이름 | Sort key | 아이디어 |
|---|---|---|
| `EDD` | `(due_date, processing_time)` | Earliest-due-date — 고전 deadline heuristic |
| `MST` | `(slack, due_date)`, `slack = due - release - proc` | Minimum-slack-time-first |
| `ERD` | `(release_time, due_date)` | Earliest-release-date |
| `LPT` | `(-processing_time, due_date)` | Longest-processing-time-first |
| `SPT` | `(processing_time, due_date)` | Shortest-processing-time-first |
| `LargestArea` | `(-area, due_date)` | Geometry-first; dense_geometry / crane_trap profile에서 강함 |
| `Midpoint` | `(release + due, due)` | 일정 창 중간점 |
| `SlackRatio` | `((due - release) / max(1, proc), due)` | 상대 slack |
| `SlackComb_Balanced` | `eval_priority_score(1, 1, 0.5)` | `α·D + β·slack − γ·P`, 균형형 |
| `SlackComb_SlackHeavy` | `eval_priority_score(0.2, 1, 0.1)` | Slack 가중 |
| `SlackComb_LPT_Heavy` | `eval_priority_score(1, 0.5, 1)` | Long job 페널티 |
| `SlackComb_HighPriority` | `eval_priority_score(0.5, 1, 1)` | 중간 균형 |

Per-seed 예산: `init_check_limit = max(0.5, timelimit * 0.30 / len(target_heuristics))`.
`timelimit=30s`에 seed 4개면 seed당 **2.25s**.

---

## 6. `evaluate_permutation`과 `evaluate_forced_permutation`

### 6.1 `evaluate_permutation(perm, search_deadline)`

1. `time.time() >= search_deadline`이면 `make_timeout_result()`로 바로 종료.
2. `baseline_greedy._place_blocks(perm, …, deadline=search_deadline)` 실행.
3. `_build_operations`로 solution dict 구성.
4. Deadline이 끊겼는데 남은 시간이 `check_feasibility`의 Shapely 비용을 흡수하기엔
   너무 적으면 (`> max(2.0, safety_margin*4)`이 아니면) raw greedy solution을 timeout
   결과와 함께 반환 — "scorer가 잘려서 init이 infeasible로 보이는" 병리를 막는다.
5. 그렇지 않으면 `_repair(repair_mode="greedy", deadline=search_deadline)` 실행.
6. operations dict를 다시 만들어 `(check_feasibility(...), sol)` 반환.

### 6.2 `evaluate_forced_permutation(perm, search_deadline)`

repair를 건너뛴다. `_place_blocks`에 `forced_ids=set(perm)`을 넘기면 모든 block이
`_empty_bay_entry` 경로로 들어가, `_force_place` docstring에 따라 (시간만 충분하면)
구조적으로 feasibility가 보장된다.

---

## 7. 초기 선택과 폴백 (H-001 이후)

```
for name in ["EDD", "SlackRatio", "MST", "LargestArea"]:
    if no_time_left: emit init.skipped; break
    perm = heuristics[name]
    res, sol = evaluate_permutation(perm, deadline = now + init_check_limit)
    emit init.heuristic_result(name, feasible, stage, objective, wall_time)
    if res.feasible and res.objective < best_obj:
        keep as best

if best_perm is None:                            # all seeds infeasible
    # H-001 (4972d5f): 진행 없이 ~20초를 태우던 edd_retry 단계를
    # 제거하고 곧장 forced placement로 간다.
    emit init.fallback(path="forced_direct", reason="all_seeds_infeasible")
    forced_res, forced_sol = evaluate_forced_permutation(EDD, hard_deadline)
    best = forced if feasible else {"operations": {}}
    emit init.fallback.outcome(path="forced_direct", ...)
else:
    emit init.chosen(name, objective)
```

`silence_stdout()` context manager가 loop을 감싸서 greedy의 print 출력을 죽인다.

---

## 8. Simulated annealing loop

### 8.1 Setup (라인 626–650)

| 변수 | 값 | 역할 |
|---|---|---|
| `search_deadline` | `min(hard_deadline - safety_margin, start_time + timelimit*0.90)` | SA wall-clock 상한 |
| `per_iter_repair_cap` | `max(2.0, timelimit*0.05)` | per-neighbor `_place_blocks + _repair` 예산; 한 neighbor가 남은 SA를 다 먹는 걸 막음 |
| `tight_blocks` | raw slack `D - R - P` 오름차순 상위 `max(3, n//3)`개 | Limited-Local-Search focus 집합, loop 전에 미리 계산 |
| `T` | 100.0 | 초기 온도 |
| `cooling_rate` | 0.97 | 반복마다 곱셈 cooling |
| Reheat threshold | `T < 0.01` → `T = 100.0` | local minima 탈출 |

### 8.2 Move 생성

```
move_type = random.choice(["swap", "insert", "invert"])

if tight_blocks is not None and random.random() < 0.50:
    idx1 = tight block 중 무작위 하나의 위치
    idx2 = 무작위
else:
    idx1, idx2 = 무작위, 무작위

move 적용
```

50% 반복은 한쪽 index를 tight-slack block에 집중, 나머지 50%는 완전 무작위.

### 8.3 Acceptance

표준 Metropolis 기준: `obj < curr_obj`면 수용 + best 갱신. 그 외에는
`exp(-(obj - curr_obj) / max(1.0, T))` 확률로 수용.

### 8.4 Event emission

- 매 새 best마다 `sa.improvement` (`iteration`, `move_type`, `objective` 포함)
- loop 종료 시 `sa.complete` (`iterations`, `improvements`, `best_objective` 포함)

주의: 거절되거나 동일 objective인 iteration은 **기록되지 않는다** — SA iteration count는
`sa.complete` event에서만 얻을 수 있다.

---

## 9. 시간 예산 구조

```
[start_time, start_time + timelimit]            ← hard deadline window
            ├─ safety_margin = clamp(0.05, timelimit*0.02, 0.5)
            ├─ 30% init phase total = init_check_limit * |target_heuristics|
            ├─ 90% search_deadline (SA cutoff)
            └─ per-iter cap = max(2.0, timelimit*0.05)
```

`timelimit=30` 기준:
- `safety_margin = 0.5`
- `init_check_limit = 2.25` (seed당) → seed 4개 다 쓰면 총 9초
- `search_deadline = min(29.5, 27.0) = 27.0`
- `per_iter_repair_cap = 2.0`

`hard_deadline`과 `search_deadline`은 `deadline=` kwarg을 통해
`baseline_greedy._place_blocks`와 `_repair`에 전파된다.

---

## 10. Event log schema

`algorithm()` 호출 전에 `OGC2026_EVENT_LOG=path.jsonl`을 설정하면 event가 기록된다.
`_emit(event, **payload)` 헬퍼가 호출당 JSON 한 줄을 append한다.

| Event | 발생 시점 | Payload |
|---|---|---|
| `algo.start` | `algorithm()` 진입 | `timelimit` |
| `algo.context` | monkey-patch 직후 | `n_blocks, n_bays, w1, w2, w3` |
| `init.start` | portfolio loop 전 | `target_heuristics, init_check_limit` |
| `init.heuristic_result` | 각 seed 평가 직후 | `name, feasible, stage, objective, wall_time` |
| `init.skipped` | 남은 시간 부족 시 | `name, reason` |
| `init.chosen` | best seed 결정 | `name, objective` |
| `init.fallback` | 모든 seed infeasible (H-001) | `path="forced_direct", reason` |
| `init.fallback.outcome` | forced placement 종료 후 | `path, feasible, objective` |
| `sa.improvement` | SA new best | `iteration, move_type, objective` |
| `sa.complete` | SA loop 종료 | `iterations, improvements, best_objective` |
| `algo.end` | return 직전 | `best_objective, wall_time, has_solution` |

`t`는 `_init_event_log(t0)`가 호출된 이후 흐른 wall-clock 초.

---

## 11. `baseline_greedy` 상호작용

Hermes는 `baseline_greedy`의 private 내부에 의존한다:

| 심볼 | 용도 | Coupling risk |
|---|---|---|
| `_place_blocks(perm, …, deadline=)` | init과 SA의 핵심 placement kernel | High — signature 변경 시 SA 망가짐 |
| `_repair(prob_info, sol, assignments, …, deadline=)` | Greedy repair pass | High |
| `_build_operations(list_of_assignments)` | 내부 tuple → solution dict 변환 | High |
| `_find_earliest_slot` | `custom_find_earliest_slot`으로 **monkey-patch됨** | Critical |
| `_active_deadline_reached()` | patch 내부 협력적 deadline 체크 | 시간 예산 정직성 필수 |
| `_time_overlaps(a1, e1, a2, e2)` | half-open interval overlap | Low |
| `_empty_bay_entry` | forced fallback 경로 | Stage-2/3 보장이 여기 달림 |

위 네 `baseline_greedy._*`의 deadline 파라미터는 모두 H-001의 scaffolding과 함께
도입되었다(commit `4972d5f`). deadline kwarg을 제거하면 Hermes의 wall-clock 정직성이
깨진다.

---

## 12. 알려진 한계와 미해결 과제

1. **b150이 forced-placement local optimum에 갇힘.** run_3 기준
   `bench_B5_b150_mixed_hard`에서 SA의 swap/insert/invert neighborhood가 forced
   solution basin을 벗어나지 못함. 후보 fix:
   - H-002 `repair_mode="simple"` seed를 SA 전에 끼워 넣어 non-degenerate 시작점 제공.
   - 더 거친 neighborhood operator (bay 재할당, bulk segment swap).

2. **Dense bench_B5에서 portfolio 4 seed 모두 stage 2 실패.**
   `tools/geometry_debug.py --probe-edd`로 확인한 바, 이는 **placement collision**
   (크레인 geometry가 아닌): block들이 서로의 footprint 안에 놓이며 overlap area
   30–56 grid unit. `custom_find_earliest_slot`의 사전 collision 체크가 hard
   instance에서 부족함을 시사 — Stage-4 pre-check가 `[entry, exit_t)`에 strictly 포함된
   block만 잡기 때문.

3. **SA per-iteration 비용이 큼 (n=120에서 ~2–3s).** 30s 안에 ~10회 정도밖에 못 돔.
   `local_polys_cache`로 Shapely 재구성 비용은 amortise됐고, 그 이상의 개선은
   neighbor마다 `_place_blocks` 전체를 다시 도는 대신 부분 재시뮬레이션이 필요함.

4. **`T < 0.01` reheat가 short run에서 너무 자주 발화할 가능성.** `cooling_rate = 0.97`
   기준 `T`가 0.01에 도달하려면 ~300 iteration 필요. 현재 bench_B5에서는 per-iter 비용
   때문에 도달하지 않음.

5. **Move 균등 가중.** `random.choice(["swap", "insert", "invert"])`가 세 move를 균등
   확률로 뽑지만 실증적 근거가 기록돼 있지 않음.

---

## 13. Maintenance rule

**이 문서와 `baseline/myalgorithm.py` / `baseline/baseline_greedy.py`는 동기화 상태를
유지해야 한다.** 다음 중 하나라도 해당되면 코드 변경과 **같은 커밋 안에서**
`ALGORITHM.md`를 함께 업데이트할 것:

| 코드 변경 | 업데이트할 섹션 |
|---|---|
| `algorithm()`의 phase 추가/제거 | §2 상위 파이프라인 |
| cache 추가/제거, anchor 변경 | §3 자료구조와 캐시 |
| monkey-patch surface 추가/제거/변경 | §4 Monkey-patches |
| heuristic 추가/제거, `target_heuristics` 변경 | §5 초기 휴리스틱 portfolio |
| `evaluate_permutation` / forced 변종 흐름 변경 | §6 |
| init 선택 또는 fallback 경로 변경 | §7 |
| SA setup, move 집합, acceptance, schedule 변경 | §8 |
| 예산 공식 변경 | §9 시간 예산 구조 |
| emit 호출 추가/제거 또는 이름 변경 | §10 Event log schema |
| deadline 전파 또는 `baseline_greedy` private symbol 사용 변경 | §11 |
| 알려진 한계 해소 또는 새로 추가 | §12 알려진 한계와 미해결 과제 |

사소한 변경(주석 편집, 포맷팅, 로그 메시지 문구)은 문서 업데이트 대상에서 제외된다.

`solver-developer` 에이전트는 가설을 구현할 때 이 규칙을 자동으로 따르도록 배선돼
있다 — `.claude/agents/solver-developer.md` 참고. 프로젝트 수준의 CLAUDE.md /
AGENTS.md 규칙도 solver를 만지는 다른 세션에 같은 메시지를 띄운다.
