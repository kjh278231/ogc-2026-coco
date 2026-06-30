# 엔진 개발 체크리스트 — 살아있는 문서

> 새 엔진(BRIDGE / PRISM / …)을 개발·튜닝·제출하기 전에 매번 점검하는 항목.
> 평가 프로토콜은 `docs/methodology.md`(eval-count 고정 + wall 별도 보고), 워크플로우는
> `docs/dev_workflow.md` 참조. **합격 기준을 못 채우면 그 자체가 다음 개선 과제.**
>
> 채점은 rank-based([[leaderboard-rank-based]]): per-instance 등수 합, infeasible/timeout/crash = −1.
> → **(A) 전 인스턴스 feasible + 시간 내**가 절대 1순위, 그 다음 (B) 폭넓은 등수 climbing.
>
> 마지막 업데이트: 2026-06-29

---

## 1. Wall time — 마진 3~5초만 (더 일찍 끝나면 유휴시간에 더 탐색)

**무엇/왜**: timelimit을 넘기면 −1(실격). 하지만 너무 일찍 끝나면 남은 예산을 버리는 것 =
등수 손해. 목표는 `timelimit − wall ∈ [3, 5]초` (안전 마진만 남기고 전부 탐색에 사용).

**어떻게 확인**:
```
# 실제 entry point + 진짜 그래더 채점(utils.check_feasibility) + wall 측정
python tools/_prism_portf_ab.py <prism|bridge> <inst> <T>   # wall_s 출력
# 여러 T(60/120/180/300)에서, 큰 인스턴스(T20/T14)와 작은 인스턴스(T2) 모두
```
**합격 기준**: 모든 (인스턴스, T)에서 `0 ≤ T − wall ≤ 5`초, overrun(음수) 0건.

**현재 상태**:
- PRISM portfolio: **✅ 적용 완료** (margin 12~20s → **4~6s**). ① master idle-reclaim ILS
  (`PRISM_PORTF_IDLE_RECLAIM`, guarded/monotonic, poly_deadline degradation으로 overrun-safe)
  ② `safety` `max(2,0.04T)`→`min(5,max(3,0.025T))`. 검증: T20@180 173.9s(margin6.1)·T14@180
  174.5s(5.5)·T17@60 55.9s(4.1), overrun 0, obj bit-identical(monotonic). 품질 불변(near-converged
  +1코어 idle-fill이 4코어 못 이김)이나 무해+미수렴 인스턴스선 도움. ⏭ 더: 워커(4코어)에 시간
  환원(final_guard/collect_margin 축소)=미수렴 상위 upside, overrun-riskier.
- BRIDGE single-process: idle-ILS로 idle ~17s→~3s 회수 완료([[idle-ils-adopted]]) ≈ 합격권.
- BRIDGE portfolio @T=180: **측정 필요**(아마 PRISM 적용 전과 유사한 10~20s 유휴 → 동일 패턴 이식 가능).

**개선 기회**:
- 포트폴리오 `final_guard`를 a_pref 빌드 ×(N+1) 대신 **실측 1회 빌드 ×작은계수**로 축소,
  `collect_margin`을 실제 워커 반환 지연 실측으로 축소 → 워커에 budget 환원.
- master가 남는 마진에 **추가 union-recombine 라운드** 또는 best 후보 ILS 연장(monotonic).
- ⚠ 단축 시 overrun 0 재확인 필수(가드 빌드가 deadline 못 넘게).

---

## 2. 리소스 — 4 CPU / 16GB RAM 충분히 사용

**무엇/왜**: 평가 서버 = 4 CPU, 16GB. 노는 코어/메모리는 버려진 탐색력.

**어떻게 확인**:
- CPU: 탐색 중 4코어가 실제로 busy한지(포트폴리오 워커 수 = 4). 단일프로세스 경로는 1코어.
  `PRISM_PORTF_DEBUG=1`/`portfolio.LAST`로 `n_workers` 확인.
- RAM: 피크 메모리(작업관리자 / `psutil`)가 16GB 대비 여유인지 — 여유면 더 큰 풀/캐시 가능.

**합격 기준**: 탐색 구간 내내 4코어 활용(병렬 경로), 단일프로세스 경로 최소화. RAM OOM 0.

**현재 상태**:
- 포트폴리오(T≥180): 4워커 = **4코어 활용** ✅ (PRISM·BRIDGE 모두).
- **단일프로세스(T<180): 1코어만 = 3코어 유휴** ❌. 게이트가 `T≥180`이라 **P1(≤60)·P2(<180)는 1코어**.
  ([[grader-p1-p6-distinct]]: P1≤60, P2∈(60,180)) → P1/P2에서 4코어 중 3개 낭비.
- RAM: **미측정**. 풀 크기 ~수만 컬럼×소 = MB 단위로 추정, 16GB 대비 큰 여유(미활용).

**개선 기회**:
- **T<180에도 병렬화**: anchor-portfolio는 워커당 풀예산(예전 budget-split 포트폴리오의 T=60
  회귀와 무관)이라 P2(나아가 P1)에서 병렬 best-of가 이득일 수 있음. **재측정 필요**
  (예전 "T=60 portfolio 회귀"는 budget-split 시절 결론 → anchor 구조엔 미적용 가능).
- RAM 여유 → `SOLVER_POOL_PER_BAY`↑(더 큰 recombine 풀), mask precompute 더 캐싱, 워커 수↑(단 4코어 cap).

---

## 3. P1~P6 문제 유형별 전략 적합성

**무엇/왜**: train T1~T40과 그래더 P1~P6은 **별개 문제**([[grader-p1-p6-distinct]], [[naming-train-T-grader-P]]).
timelimit 맵: **P1≤60 / P2∈(60,180) / P3~P6≥180**. 목적은 Z3-지배([[objective-z3-dominant]]),
하드-패킹(P4~P6류)이 BRIDGE 약점([[grader-best-0619-2]]).

**어떻게 확인**:
- 게이트가 timelimit 맵과 일치하는지(코드 리뷰): `timelimit≥180 → 병렬/강레버`, 미만 → 안전.
- 전 인스턴스 feasible(stage 5) + overrun 0 (`utils.check_feasibility`).
- 인스턴스 타입 적응: 저-tardy(Z3중심) vs 하드-패킹(Z1중심)에 다른 전략?

**합격 기준**: (A) 전 인스턴스 feasible·시간 내, (B) 하드-패킹 + Z3중심 양쪽에서 경쟁력,
(C) 짧은 예산(P1/P2)에서도 강한 단일/병렬 해.

**현재 상태**:
- 게이트(T≥180 병렬) = timelimit 맵 정합 ✅. feasibility fallback(AABB) ✅.
- **PRISM이 하드/대형/Z3(P3~P6류)에서 BRIDGE 포트폴리오 −9%(하드-8)** ✅ — 정확히 약점존 공략.
- P1/P2(짧은 예산): 단일프로세스 = 항목2 미흡. P1은 [[bridge-third-long-budget]]상 BRIDGE 우위.
- 타입 적응: 제한적. MIP-repair/MIP-anchor를 **항상** 실행(저-Z3 인스턴스선 탐색 강탈 가능,
  계획 0★b "입력 기반 MIP-repair 게이트" 미완). adaptive-R(계획 1)도 미완.

**개선 기회**:
- **그래더 검증 필수**: train 승리가 P1~P6로 전이되는지 제출로 확인([[anchor-to-grader-best]]).
- 입력 신호(면적 utilization·m·a_pref Z2불균형)로 **타입별 전략 분기**(MIP-repair gate, R 선택).
- P4~P6(최대 목적·eval-starved): 패킹 perf(항목4)·병렬·recombine이 직접 등수에 기여.

---

## 4. Bit-identical 속도 개선 (남은 부분)

**무엇/왜**: 스택이 eval-제한적 → 같은 wall에 eval↑ = Z3↓. **behavior-invariant(고정-eval
bit-identical obj)** 만 안전([[eval-count-ab-protocol]]). cProfile self-time은 Python 오버헤드
과대계상하므로 **순수 속도는 고정-eval wall A/B로 판정**([[cprofile-selftime-misleads-purespeed]]).

**어떻게 확인**:
```
python tools/_profile_speed.py <inst>      # hot spot 프로파일
python tools/_time_eval.py <inst> <E>      # 고정-eval wall A/B (전/후)
# 게이트: 후보 변경이 전 인스턴스 obj BIT-IDENTICAL인지(E 고정 2~3회 일치) 먼저 확인
```
**합격 기준**: 변경 후 obj가 모든 인스턴스에서 bit-identical, wall만 단축(무거운/eval-starved
인스턴스 T37/T38류에서 회귀 없는지 — 작은 인스턴스만 보면 안 됨).

**현재 상태** (대부분 소진):
- 적용 완료: numba(find_slot/scan/masks_overlap), shapely.prepare, find_slot_mask 스캔 jit,
  per-instance bbox/local-mask 캐시, bbcache([[bridge-bbcache-speedup]]).
- mask-marshal 재구조: bit-identical이나 wall 이득 marginal → 미채택([[cprofile-selftime-misleads-purespeed]]).
- **남은 실표적 = shapely crane-check**(`check_entry`/`check_exit`, 실 C work ~13s) — 단
  feasibility-critical("Target D")이라 고위험.
- PRISM 고유 오버헤드: MIP 앵커 solve(~0.02s, 무시 가능)·ILS 루프(=BRIDGE와 동급) — **프로파일 필요**.

**개선 기회**:
- crane-check 근사/캐시(보수성 유지 = false-negative 0 필수, 겹치는데 안겹친다 하면 실격).
- mask precompute 래스터화 numba화(계획 2a, supercover 안전성 재검증).
- PRISM `_score_and_pack` 최종빌드 1회화는 이미 mask-only(BRIDGE 상속).

---

## 제출 전 빠른 체크리스트 (요약)

- [ ] **1. Wall 마진 3~5초** — 대/소 인스턴스, 대상 T 전부 `0 ≤ T−wall ≤ 5`, overrun 0.
- [ ] **2. 4코어 활용** — 병렬 경로 `n_workers=4`; 단일프로세스 경로(P1/P2) 병렬화 검토; RAM OOM 0.
- [ ] **3. 전 인스턴스 feasible(stage 5)** + 게이트가 timelimit 맵 정합 + 하드/Z3 양쪽 경쟁력.
- [ ] **4. 속도 변경은 고정-eval bit-identical** 게이트 통과(무거운 인스턴스 포함) 후만 채택.
- [ ] **제출 zip**: `myalgorithm.py` 루트 평면, ≤15MB, 추출본에서 spawn+feasible+overrun無 스모크(`tools/_zip_smoke.py`).
- [ ] **개선 판정 baseline = 사용자 실제 그래더 best**(세션 자체 제출본끼리 비교 금지, [[anchor-to-grader-best]]).
