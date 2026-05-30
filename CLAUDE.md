# CLAUDE.md

이 파일은 Claude Code (claude.ai/code)가 이 repo에서 작업할 때 따라야 할
가이드를 제공한다.

## Project Overview

OGC 2026 (Optimization Grand Challenge) — 조선소 block-stowage 스케줄링 문제.
Solver는 block을 사각형 bay에 `(x, y, orient, entry_time, exit_time)` 결정으로
배치하고, feasibility checker가 solution을 검증한 뒤 가중 objective
`w1*tardiness + w2*load_imbalance + w3*bay_preference_penalty`를 계산한다.

> **Algorithm reference**: 현재 Hermes solver의 end-to-end 설명(pipeline, cache,
> monkey-patch, heuristic portfolio, SA loop, time budget, event schema, 알려진
> 한계)은 [ALGORITHM.md](ALGORITHM.md)를 볼 것. `baseline/myalgorithm.py` 또는
> `baseline/baseline_greedy.py`를 수정할 의도가 있다면 **먼저** 이 문서를
> 읽을 것.
>
> **Auto-sync rule**: `baseline/myalgorithm.py` 또는 `baseline/baseline_greedy.py`의
> 변경이 ALGORITHM.md §13의 트리거 체크리스트 항목을 건드리면, 같은 커밋
> 안에서 ALGORITHM.md의 해당 섹션을 **반드시** 갱신해야 한다. 사소한 변경
> (주석, 포맷팅, 로그 문구)은 doc 업데이트 불필요. solver-developer 에이전트는
> 이 규칙을 자동으로 강제하며, solver를 직접 편집하는 세션도 같은 규칙을
> 따라야 한다.

## Environment

```bash
conda env create -f ogc2026_env.yml
conda activate ogc2026
```

Python 3.12, PyQt6 GUI, geometry는 Shapely. 무거운 옵션 deps로 Gurobi, Xpress,
OR-Tools, Torch, TF 포함. Numba와 OpenJDK도 함께 들어온다.

## Common Commands

```bash
# GUI tester 실행 (instance + algorithm 폴더 선택 후 Run 클릭)
conda activate ogc2026
cd alg_tester && python alg_tester_app.py

# Headless 일괄 평가: 모든 benchmark JSON에 대해 baseline_greedy와 myalgorithm
# 을 돌리고 비교 표를 출력.
python evaluate_all.py --timelimit 60 --greedy-timelimit 10
python evaluate_all.py --pattern "smoke_*.json" --output results.json

# Benchmark suite 생성 (alg_tester/example/benchmark/에 기록)
python alg_tester/example/generate_benchmark_suite.py --suite smoke
python alg_tester/example/generate_benchmark_suite.py --single --name my_dense \
    --bays 5 --blocks 120 --profile dense_geometry
```

테스트 프레임워크, lint 설정, 빌드 단계가 없다 — benchmark instance에 대한
평가가 곧 테스트 loop이다.

## Architecture

### Contestant contract

`baseline/myalgorithm.py`는 함수 하나 `algorithm(prob_info: dict, timelimit:
float) -> dict`를 export한다. **signature 변경 금지.** `baseline/` 안의 나머지는
편집 가능; `utils.py`는 공식 채점 코드이므로 수정 불가.

Solution dict 형식은 [baseline/baseline_greedy.py](baseline/baseline_greedy.py)
상단에 완전히 문서화돼 있다: 정수 time-as-string으로 key를 만든 flat
`operations` dict, 같은 timepoint에서 EXIT가 ENTRY보다 먼저. ENTRY는 `(bay_id, x,
y, orient_idx)`를, EXIT는 `(bay_id, block_id)`를 들고 다닌다.

### Physical model ([alg_tester/utils.py](alg_tester/utils.py) header 참고)

- Bay는 정수 격자의 사각형. Block은 *multi-layer* polygon (layer 0이 가장 낮은
  물리 level).
- 크레인은 수직으로만 움직이므로 collision check는 **`j >= k` descent-path
  rule**을 쓴다: 새 block의 layer `k`가 내려갈 때, 기존 block의 `j >= k`인 모든
  layer 높이를 sweep한다. 그래서 `check_entry`/`check_exit`/`check_collisions`는
  단순한 AABB 테스트가 아니라 3D에서 layer별 Shapely intersection이 필요하다.
- `check_feasibility`는 5단계를 순서대로 확인: (1) assignment validity, (2)
  crane entry, (3) crane exit, (4) spatial collisions + boundary, (5) sequential
  operation replay. 반환된 `stage`는 *최초로 실패한* stage이지 유일한 실패는
  아닐 수 있다.

### Duplicate utils.py — 주의

`baseline/utils.py`와 `alg_tester/utils.py`는 바이트 단위로 동일하다. tester는
자기 것을 import; `myalgorithm.py`와 `baseline_greedy.py`는 `baseline/`의 것을
import. `utils.py`를 수정할 일이 있다면 둘을 동기화해야 한다 — 다만 README는
contestant가 수정하면 안 된다고 명시함.

### Baseline greedy 구조

[baseline/baseline_greedy.py](baseline/baseline_greedy.py)는 reference solver이며
`myalgorithm.py`가 광범위하게 재사용한다. 주요 내부 진입점 (private이지만
바깥에서 호출됨):

- `_place_blocks(...)` — 공유 placement kernel (Phase 1과 repair loop에서 사용).
- `_find_earliest_slot(new_blk, bay, placed_in_bay, schedule_in_bay, r_time, proc)`
  — crane-feasible time-slot 탐색. **`myalgorithm.py`가 추가 체크 주입을 위해
  monkey-patch한다.**
- `_repair(...)` — feasibility 실패 block의 반복 재배치; `"greedy"`와 `"simple"`
  모드 지원.
- `_build_operations(assignments)` — 내부 `(bay, x, y, orient, entry, exit)` tuple을
  공식 `operations` dict로 변환.

### myalgorithm.py 전략 (현재 Hermes solver)

상세는 [ALGORITHM.md](ALGORITHM.md) 참고. 미래의 Claude 인스턴스가 알아야 할
핵심 self-non-obvious 사항:

1. **OBB precompute + cache** (`precompute_obbs`, `obb_cache`) — `(block_id,
   orient_idx)`로 키. 각 shape의 minimum rotated rectangle을 *local* 좌표로 저장.
   `get_world_obb(block)`이 `(block.x, block.y)`로 lazy translate하고
   `block._world_obb`에 memoise.
2. **`algorithm()` 상단에서 `utils.check_entry/check_exit/check_collisions`와
   `baseline_greedy._find_earliest_slot`을 custom 3-stage filter (AABB → OBB →
   full Shapely)로 monkey-patch**. 원본은 module import 시점에
   `original_check_entry` 등으로 캡처되고 **return 전에 복구된다** — 그래야
   patch가 `evaluate_all.py`의 다음 iteration으로 새지 않는다. 새 patch를
   추가하면 복구도 함께 추가할 것.
3. **12개 named permutation heuristic portfolio** (`EDD`, `MST`, `ERD`, `LPT`,
   `SPT`, `LargestArea`, `Midpoint`, `SlackRatio`, `SlackComb_*`) — 다만 현재
   초기 seed로 평가되는 것은 `target_heuristics = ["EDD", "SlackRatio", "MST",
   "LargestArea"]` 4개. SA에 더 많은 wall-clock을 주기 위해서.
4. **Permutation 공간에서의 Simulated Annealing**, `swap`/`insert`/`invert`
   move, tight-slack block에 50% 확률 집중, `T < 0.01`에서 reheat, `timelimit`의
   90% 예산. 한 neighbor 평가가 전체 greedy `_place_blocks` + `_repair`
   파이프라인을 다시 도는 것을 의미한다 — 비싸므로 OBB caching이 중요하다.
5. `evaluate_permutation`은 `_place_blocks` 다음 `_repair`를 호출하며 둘 다
   patched `_find_earliest_slot`을 기대한다. `evaluate_all.py`는 각
   `greedyalgorithm` 호출 전에 patch를 방어적으로 재초기화한다(라인 55–58 참고)
   — baseline은 unpatched 상태로 돌도록.

### Benchmark instances

`alg_tester/example/` 구성:
- `example_B2_b10.json` — 작은 시드 instance (2 bay, 10 block).
- `generate_large_example.py`와 `generate_benchmark_suite.py` — `balanced`,
  `tight_due`, `dense_geometry`, `crane_trap`, `preference_skew`,
  `workload_balance` 같은 profile의 instance 생성기.
- `benchmark/` — `evaluate_all.py`가 순회하는 생성된 suite.

파일 이름의 `B<n>` = bay 수; `b<n>` = block 수. `smoke_*`는 빠른 sanity
instance; `bench_*`와 `my_B5_b200_hard.json`은 더 어려운 run.

## 문서 작성 규칙

코드 외 markdown 산출물(ALGORITHM.md, progress.md, agent/skill prompt,
README 등)은 **한글로 작성**한다. 단, 다음은 영어 유지:

- 기술용어 (monkey-patch, OBB cache, Stage 2, simulated annealing, descent
  rule 등)
- 코드 블록과 명령어
- 파일/함수/심볼 이름
- 인용된 event 이름이나 JSON 키

agent frontmatter의 `description` 필드는 라우터의 트리거 detection을 위해
한국어 / 영어 표현 두 가지를 모두 포함시킨다.
