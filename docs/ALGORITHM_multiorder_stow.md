# 이 알고리즘이 무엇인가 — multi-order 적치 + PRISM+MO + STOW (2026-07-01)

이 문서는 2026-07-01 작업의 결과물을 설명한다. 한 줄 요약:

> **블록을 bay에 "적치(placement)"하는 순서를 한 가지(EDD)로 고정하지 않고 여러 원칙적
> 순서의 best-of로 바꾸는 새 패킹 기법(multi-order)을 만들고, 이를 기존 챔피언 PRISM에
> 결합해(PRISM+MO) 역대 best 대비 −31%를 달성했으며, 같은 아이디어를 "패킹 정책 다양성"
> 포트폴리오로 일반화한 신규 솔버 STOW(`stow/`)를 추가했다.**

---

## 1. 문제 배경 (OGC 2026)

- **bay**: 폭×높이의 직사각형 적치장. 인스턴스마다 여러 개(m≈3~5).
- **block**: 다각형(여러 layer로 된 단면) 형상을 가진 물체. 각 블록은
  `release_time`(반입 가능 시각), `processing_time`(체류 시간), `due_date`(반출 기한),
  `workload`(작업량), `bay_preferences`(bay별 선호 점수)를 가진다.
- **크레인**: **수직(z) 전용**. 블록을 똑바로 내려 적치(ENTRY)하고 똑바로 들어 반출(EXIT)한다.
  내리는 중 다른 블록과 부딪히면 안 된다(j≥k 스윕 규칙).
- **목적함수(최소화)**: `w1·Z1 + w2·Z2 + w3·Z3`, 거의 사전식(near-lexicographic)으로
  **w1 ≫ w3 ≫ w2**.
  - **Z1 = 총 지각** `Σ max(0, exit_time − due_date)`
  - **Z2 = 정규화 부하 불균형**(bay 쌍 최대 격차)
  - **Z3 = 선호 페널티** `Σ (최대선호 − 배정된 bay의 선호)`

핵심 구조(코드·데이터로 확인): **Z2·Z3는 배정(assignment)만의 함수**(어느 블록이 어느
bay인가)이고 패킹이 필요 없다. **오직 Z1만 패킹(적치)에 의존**한다.

---

## 2. 핵심 진단 — "지각(Z1)은 100% 혼잡이다"

`tools/_pack_diag.py`로 측정한 결정적 사실들:

1. **temporal_floor(Z1) = Σ max(0, R+P−D) = 0** (전 train 인스턴스).
   → 어떤 블록도 자기 release 시점에 "구조적으로" 늦지 않는다. 즉 **Z1>0은 전부, bay가
   붐벼서 패커가 진입(entry)을 release보다 늦게 미룬 결과**(혼잡, contention)다.
2. 공존(같은 시간대) 블록들은 패커가 footprint를 서로 겹치지 않게 배치하므로, 크레인
   반출은 항상 가능하다 → **Z1 = Σ max(0, entry + proc − due)**, 즉 **진입 시각만으로
   결정**된다.
3. 따라서 **적치를 잘해서 블록들을 더 일찍/촘촘히 들여보내면 Z1이 줄어든다.**

기존 모든 솔버(BRIDGE/PRISM/ALNS)는 **하나의 고정 결정론적 패커**(`solve_bay`)를 공유했다.
그 패커는 블록을 **EDD 순서**(`due_date`, `processing_time`)로 정렬해 차례로
**bottom-left first-fit**으로 넣는다. 즉 **탐색은 "배정"만 바꾸고 "적치"는 한 번도 탐색
대상이 된 적이 없었다.** 사용자가 지목한 "적치 개선"의 빈틈이 바로 여기였다.

---

## 3. 새 적치 기법 — multi-order best-of 패커 (`SOLVER_MULTIORDER`)

### 아이디어
Z1이 혼잡이라면, **누가 먼저 좋은(이른) 슬롯을 차지하는가 = 넣는 순서**가 Z1을 좌우한다.
EDD가 늘 최선은 아니다. `tools/_order_probe.py`로 측정: bay마다 최적 순서가 다르고,
**bay별 최적 순서(oracle) vs EDD = Z1 −24%**, 단일 `release` 순서만으로도 −19%였다.
무작위 순서는 오히려 EDD보다 나빴다 → **"원칙적 순서들의 best-of"가 정답.**

### 구현 (`bridge/packing.py`)
- `solve_bay(..., order=None)`: 넣는 순서를 주입 가능하게 함. `order=None`이면 기존 EDD →
  **기본 경로는 bit-identical**(무변경).
- `solve_bay_best(prob, j, ids, ...)`:
  1. 먼저 **EDD**로 패킹하고 지각 T를 계산.
  2. **T=0이면 그대로 반환**(추가 비용 없음 — 대부분의 bay).
  3. T>0(혼잡한 bay)일 때만 **release / least-slack / area-desc** 순서로도 패킹해
     **지각이 가장 적은 배치를 채택**.
  - EDD가 항상 후보에 있으므로 **결과는 EDD보다 절대 나빠질 수 없다(per-bay Pareto-safe)**,
    각 후보가 완전한 유효 패킹이므로 feasibility도 보존된다.
- 순서 집합은 `SOLVER_MO_ORDERS`로 조절(기본 4개; 실험 결과 4-order가 2-order보다 우수).

### 탐색과의 결합 (`bridge/solver.py`)
multi-order를 **탐색 평가(eval_obj1)·recombine 가드(_bestof_obj)·최종 빌드(_score_and_pack)
3곳 모두**에 일관되게 연결했다(중간에 기준이 어긋나는 "proxy seam"이 없도록). 그 결과
**탐색이 더 정확한(더 낮은) Z1을 보게 되어, 같은 Z1=0을 유지하면서 더 선호도 높은(Z3가
낮은) 배정을 고를 여유가 생긴다.** 즉 적치 개선이 지배 비용인 Z3까지 끌어내린다.

### 비용/주의
tardy bay마다 최대 4번 패킹하므로 **per-eval이 2~3배 느리다**. 고정 eval 수에서는 거의
항상 큰 품질 승(T6 −64% 등)이지만, 한 번의 wall(시간) 단일 실행에서는 eval을 덜 하게 되어
드물게 손해날 수 있다(궤적이 비단조). → **그래서 포트폴리오(여러 워커가 시간 끝까지)에서
best-of로 쓰는 것이 정답.** 단일프로세스 블랭킷 기본값으로는 부적합.

---

## 4. PRISM+MO — 기존 챔피언에 결합 (실제 배포 형태)

PRISM은 "preference-ideal MIP 앵커 스펙트럼 + LAHC" 솔버로, 4코어 포트폴리오로 동작한다.
`SOLVER_MULTIORDER`는 PRISM의 공유 커널(`K.total_obj`/`K._score_and_pack`)을 그대로
거치므로, **`prism/myalgorithm.py`에 `SOLVER_MULTIORDER=1` 한 줄을 켜는 것만으로** 마스터와
spawn 워커 전부가 multi-order 패킹을 쓴다.

이것이 모든 레버를 합친다: **PRISM의 MIP 앵커**(특정 인스턴스의 좋은 배정 basin) **+
multi-order 패킹**(혼잡 Z1을 줄여 더 좋은 배정 도달).

### 검증 (T=180 포트폴리오, 진짜 그래더 obj, 6 하드 인스턴스)
| | 집계 | vs PRISM |
|--|--:|--:|
| BRIDGE | 665,545 | — |
| PRISM(역대 best) | 605,131 | 기준 |
| STOW(아래) | 550,292 | −9% |
| **PRISM+MO** | **417,043** | **−31%** |
| BRIDGE+MO | 440,799 | −27% |

PRISM+MO는 **6/6 전승**(T1 −80%, T13 −34%, T20 −33% 등). T20/T13 같은
하드패킹(=그래더 약점존 P4-P6) 직격. **전 train ~20개 인스턴스에서 회귀 0, T=300에서
overrun 없음**으로 검증됨. 과거 PRISM이 약했던 packing-sensitive **T4를 −63%**로 해결.

배포: `prism/myalgorithm.py`·`bridge/myalgorithm.py`에 기본 활성화(env로 롤백 가능).
제출본: `myalgorithm0701-prism-multiorder.zip`.

---

## 5. STOW — 신규 솔버 (`stow/`, packing-diverse 포트폴리오)

같은 적치 아이디어를 **하나의 독립 기법**으로 일반화한 것. 기존 솔버의 다양성 축은
BRIDGE=탐색 seed, PRISM=MIP 앵커였는데, **STOW의 다양성 축은 "패킹 정책" 자체**다.
4개 워커 중 일부는 multi-order로, 일부는 EDD로 탐색하고, 마스터가 진짜 점수로 best-of한다.
이는 multi-order의 비단조성을 best-of로 안전하게(회귀 없이) 흡수한다.

STOW는 단독으로 BRIDGE/PRISM을 능가한다(집계 550K). 다만 MIP 앵커가 없어 일부 인스턴스
(T11)에서 PRISM+MO에 못 미친다 → **현재 최강 배포는 PRISM+MO**, STOW는 "패킹을 다양성
축으로 쓴다"는 신규 기법의 검증·확장 토대(추후 MIP 앵커 결합 시 PRISM+MO로 수렴).

---

## 6. 검토했지만 채택하지 않은 것 — 맞물림(interlocking)

블록의 82~98%가 멀티레이어이고 34~57%가 stepped(상단이 하단보다 돌출)라, 크레인 모델상
overhang을 더 낮은 이웃 위로 거는 **맞물림 배치**가 이론상 가능하다(현 패커는 union
footprint를 통째로 비겹침 요구해 이를 못 씀). 그러나 `tools/_interlock_ceiling.py`로
**천장을 falsify**: Z1을 지배하는 포화 bay는 바닥(layer-0) 면적만으로도 포화 상태라
맞물림으로 못 풀고, 나머지는 이득이 8~13%로 작다. feasibility-critical(겹치는데 안
겹친다고 하면 실격)인 위험 대비 가치가 낮아 **비우선**으로 둠.

---

## 7. 코드 위치 / 사용법

- 적치 커널: `bridge/packing.py` (`solve_bay(order=)`, `solve_bay_best`, `_order_candidates`)
- 탐색 연결: `bridge/solver.py` (`_MULTIORDER` → eval/guard/build)
- 활성화: `bridge/myalgorithm.py`·`prism/myalgorithm.py`의 `SOLVER_MULTIORDER`(기본 ON)
- 신규 솔버: `stow/myalgorithm.py`
- 환경변수:
  - `SOLVER_MULTIORDER=0/1` — multi-order 패킹 on/off (기본 1)
  - `SOLVER_MO_ORDERS="edd,release,leastslack,areadesc"` — 사용할 순서 집합(기본 4개)
- 실행 예: `python tools/_prism_portf_ab.py prism T20 180` (PRISM+MO, 진짜 obj로 채점)
- 진단·A/B 도구: `tools/_pack_diag.py`, `_order_probe.py`, `_interlock_ceiling.py`,
  `_mo_*.sh`, `_solver3_ab.sh`
- 상세 실험 기록: `docs/stow_experiment_log.md`, `docs/experiment_board.md`

## 8. 다음 레버 (그래더 결과 확인 후)
- snapshot-guard: multi-order 비단조 궤적을 다중 체크포인트 진짜채점으로 흡수(단조 보장).
- position/orientation 정책: 적치의 남은 축(배치 위치 규칙·방향 선택) 탐색(미탐색).
