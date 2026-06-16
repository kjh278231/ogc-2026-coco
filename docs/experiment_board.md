# 실험 보드 — 살아있는 상태판 (계획 / 진행 중 / 완료)

> 솔버 실험의 **현재 상태 인덱스**: 무엇이 대기 중이고, 무엇이 실행 중이며, 무엇이 결론났는지.
> 상세 서술·수치는 `docs/experiment_log.md`(BRIDGE)와 `docs/third_algorithm_experiment_log.md`(covering)에,
> 오래 가는 결론은 `memory/`에 있다. **이 보드는 앞으로 할 것 + 지금 도는 것 추적용**이다.
> 항상 최신으로 유지할 것: 실험 시작 → 진행 중, 끝나면 → 완료(결론과 함께), 새 아이디어는 → 계획.

**마지막 업데이트: 2026-06-16**

---

## 🔵 진행 중 (지금 실행 중)

| 시작 | 실험 | 설정 | 출력 파일 | 무엇을 결정하나 |
|---|---|---|---|---|
| (현재 없음) | | | | |

---

## 🟡 계획 (우선순위순)

0. **full-20 재확인 + 기본값 플립** — 스택에 numba 기본 on(=−28.5% 추가). 전 20개로 새 합산·회귀 재확정 후 mask-search/자동조절/numba를 default on으로 코드 플립 + submission 폴더 갱신.
1. **adaptive R (입력 기반 R4/8/16 자동선택)** — instance마다 최적 R 다름(prob_3은 R16까지 개선, prob_20은 R4로 충분). 입력에서 싼 신호로 판단: over-rejection 수렴(knee) / footprint·AABB 면적비 / 크기·예산. 먼저 측정: 전 20개 oracle-best R + 예측신호 상관 → 규칙화. (eval-제한적이라 거친 instance에 낮은 R = 더 빠름 = 더 좋음.)
2. **남은 속도 (eval-제한적이라 큰 objective 전환)** — (a) **mask precompute ~39% 가속**(shapely 래스터화 → numba point-in-poly/matplotlib path, supercover 안전성 재검증 필수), (b) **find_slot_mask 스캔 coarse numba화**(현재 호출당 → 통째 jit로 경계비용 제거).
3. **prob_3 회귀 원인 규명** — eval-count 무관(numba로도 불변). R/basin 문제로 추정.
1. **재실험 #2: guided ILS (`SOLVER_GUIDED`) under mask-search** — 과거 prob_17/18 +32%/+20% 회귀. guided가 tardy-bay를 AABB로 오판한 게 원인 의심 → mask-search(거의-진짜 신호) 하에서 재시도. flip 가능성.
2. **recombine 재검토 under mask** — 프로파일상 recombine 가드가 polygon 사용(~8s)이고 prob_20에선 개선 0. mask-search 하에서 가드를 mask로 바꾸거나 recombine 축소/생략 검토(시간 회수).
3. **재실험 #3: H2 search→recombine→search 루프 under mask-search** (얇은 AABB pool이 폐기 사유였음).
4. **탐색 중 incumbent 스냅샷 진짜채점** (어긋남 보정 — **자동조절 채택의 관문, 최우선**) — 지금은 끝에서 2개(탐색 전/후 최선)만 진짜 점수로 비교. 이걸 **탐색 도중 여러 시점의 최선 답 스냅샷**으로 늘려서 전부 진짜채점하고 best 제출. 그러면 "더 탐색"이 "덜 탐색(=이른 스냅샷)"보다 절대 나빠질 수 없음 → **단조 보장 → prob_7(+48%)·prob_6 회귀를 잡음.** (주의: proxy 순위 top-K만 모으면 true-good이 proxy 순위 낮아 누락될 수 있으니, **시점 체크포인트** 기반이 안전.) 이게 되면 자동조절(−9.1%)을 안전하게 켤 수 있음.
2. **`find_slot` 추가 가속** (numba 기본형 ~2× 확보 후) — 호출당 오버헤드 제거: `ov_boxes`를 numpy로 사전계산/버퍼 재사용, 또는 `solve_bay` 단위로 더 굵게 jitting해 경계 비용 분산. (behavior-invariant, 평가횟수 고정으로 검증.)
3. **polygon "상금" 측정** — `SOLVER_NOPOLY=1` vs 기본, full-20 @ T=180. 차이 = polygon이 지금 회복하는 양 = "더 나은 기하 테스트"가 노릴 상금의 하한. 싸다. 4번 진행 여부를 가름.
4. **거의 정확하면서 빠른 기하 테스트** (멀티박스/래스터) — 탐색과 최종을 한 기준으로 **통일** → 어긋남 제거 + AABB가 못 보던 맞물림 배치까지 탐색. 큰 보상·큰 위험. **보수적 유지 필수**(겹치는데 안 겹친다고 하면 실격 = feasibility). 2번 상금이 크면 진행.
5. **병렬 재시작 모음** (multiprocessing, ≤4코어) — 독립 ILS 여러 개 동시에 → 진짜 점수로 best-of. 탐색 중 노는 3코어 활용. 4로 캡(`os.cpu_count()`는 cpulimit 환경에서 못 믿음). 1번과 곱셈 효과.

---

## 🟢 완료 (최근 — 결론 + 링크)

- **numba payoff (wall A/B, T=120)** — 스택 numba off vs on: **합산 −28.5%**, 전부 feasible. **prob_18 회귀 해결+α**(+17.9%→기본보다 −50%, eval-count가 원인 확인), prob_13 −47%, prob_20 −16%. **prob_3만 불변(+0%)** = eval-count 무관 별도 원인. **→ numba 스택 기본 on.** 스택이 eval-제한적이라 속도→objective 전환이 큼(=남은 속도 표적 가치↑). → `memory/bitmask-collision-result.md`
- **numba `masks_overlap` (uint64 패킹) + find_slot AABB** — `_local_mask`에 uint64 rows 추가, numba `_masks_overlap_u64`, `masks_overlap`이 `SOLVER_NUMBA` 시 dispatch. **behavior-invariant 확정**(big-int vs numba 15000 케이스 R4/8/16 모두 mismatch=0; eval-count obj 완전 동일). 스택 end-to-end **~1.56× 속도**(prob_13 41→26s, prob_20 64→40s). masks_overlap precompute는 미가속(별개 표적). → `memory/bitmask-collision-result.md`
- **스택 full-20 확인 (채택 관문)** — 현재 기본 vs 스택(mask-search+자동조절 R8) @ T=120: **합산 −40.2%**(9.81M→5.87M), **18승 2패, 전부 feasible.** 회귀 prob_18 +17.9%·prob_3 +11.8%(둘 다 evals 1/4 손실). **→ 채택 확정**(기본값 전환). 역대 최고, default objective를 거의 절반으로. → `memory/bitmask-collision-result.md`
- **프로파일링 (prob_20 스택)** — 단계: seed 真값 5.14M → improved/local search(각 ~36s)가 1.61M(개선의 거의 전부), ILS 0s(예산 소진), recombine 11s 무익. 코드 점유: **mask precompute(shapely 래스터화) ~39% #1 비용**, masks_overlap ~12%, find_slot AABB ~9%, recombine 가드가 polygon 사용(~8s). → 최적화 표적: ①mask precompute(R↓/래스터화 가속) ②masks_overlap numba화 ③recombine 재검토. evals 회복이 prob_3/18 회귀 해소 경로.
- **재실험 #1: 자동조절 + mask-search** (A/B, T=120, 7개) — **drift 이론 확정.** AABB 때 회귀였던 prob_7(+48%→+0%)·prob_6(+18.5%→−68.8%)·prob_11(+23.7%→−15.4%)가 mask-search 하에선 회귀 소멸/개선. 자동조절이 mask-search 위에 **−12.5% 추가(회귀 0, 전부 feasible)**. → 과거 "X 회귀" 판정 재검증 프로그램 정당화. → `memory/bitmask-collision-result.md`
- **mask를 탐색 scoring에 적용 (A/B, T=120, 8개)** — `SOLVER_MASK_SEARCH`. 기본 vs mask-search 통합: **합산 −34.5%, 전부 feasible.** prob_20 −52.5%, prob_7(drift) −31.3%, prob_6 −31%, prob_11 −20%, prob_5 −18%; 회귀는 prob_3 +11.8%(evals 1/4 손실)뿐. **드리프트 해소 + 맞물림 탐색 = 변혁적 lever.** → `memory/bitmask-collision-result.md`
- **bitmask(supercover) 충돌 모델 구현·검증** (`SOLVER_MASK`/`SOLVER_MASK_R`, 기본 off) — 안전성 ✓(false-negative=0, R4/8/16), 최종빌드 A/B 전부 feasible + mask R8 ≈ polygon 품질. **속도(같은 배치 빌드, 정상상태): mask ~3–4× AABB vs polygon ~50–65× → mask가 polygon보다 ~15× 빠름.** precompute 일회성(R8 ~2–9s). → mask는 탐색에 쓸 만큼 쌈 ⇒ 계획 #0. → `memory/bitmask-collision-result.md`
- **`find_slot` numba 포팅** (`SOLVER_NUMBA`, env-gated, 기본 off) — 평가횟수 고정에서 **결과 bit-identical**(prob_5/13/17) = behavior-invariant 증명 ✓ (안전). 속도: search만 **~2.2×**(prob_13 NOPOLY 36→16s), polygon 포함 ~1.5×. 기대(10×)보다 작음 — **호출당 Python 전처리(ov_boxes bounding_rect + np.asarray)+numba 진입 오버헤드**가 천장. 안전한 ~2× 확보지만 transformative 아님. (로컬 .venv에 numba 0.65.1 설치; 평가 env는 0.61, 결과는 버전 무관.) 더 키우려면 → 계획 #1.
- **예비시간 자동조절 full-20 비교** (기본 vs poly만 자동조절, T=180) — 합산 **−9.1%** (8,594,557 → 7,814,220), 10승 4패 6무, 전부 feasible. 큰 이득: prob_19/11/20/13/14 (−13~17%). **그러나 어긋남 회귀 prob_7 +48%(!) · prob_6 +8%** → **단독 채택 보류.** 채택은 계획 #1(스냅샷 진짜채점)으로 회귀를 잡은 뒤. → `memory/bridge-third-long-budget.md`
- **prob_7 +48% 회귀 = 진짜 (variance 아님)** — 기본 106,613 ×3, 자동조절 157,916 ×3, **둘 다 0% 편차(완전 결정적)**. 더 많은 탐색이 재현 가능하게 48% 악화 = 심각한 어긋남. → 자동조절 채택은 계획 #1이 선결. → `memory/bridge-third-long-budget.md`
- **warm-start graft (BRIDGE → third 풀생성)** — **반증.** graft@180이 그냥 BRIDGE@180보다 4개 전부 나쁨(+10.2%). third의 승리는 자기 전체 탐색이 필요 → graft 아니라 portfolio가 맞음. → `memory/bridge-third-long-budget.md`
- **장기 budget 스케일링 + idle tail 원인** — BRIDGE는 T=300까지 미포화(prob_20 180→300 −32%). ~45% idle tail의 원인 = 가속 이전에 맞춰진 낡은 예비시간(poly 0.30 + recomb 0.18)이 탐색을 굶김. → 같은 메모리
- **예비시간 줄이기 (고정 저값)** — poly 0.10/recomb 0.07 @ T=300: 4개 합산 **−18.2%**, 전부 feasible. 레버 확인.
- **예비시간 자동조절 구현** (`SOLVER_ADAPTIVE_RESERVE`, env-gated, 기본 off) — poly 예비시간을 실측 build 비용으로 조절. recombine까지 조절하면 굶는 결함 발견·수정(poly만 조절; prob_11 +23.7% → −14%). **어긋남(proxy-drift) 발견**(prob_6 +8%, 재현됨): 탐색을 더 한다고 항상 좋아지는 게 아님. → 같은 메모리
- **standalone 최강 = BRIDGE** (vs third/covering). third는 상보적(T=180에서 prob_11/20 승). → 같은 메모리

---

*상세 과거 서술은 `docs/experiment_log.md` · `docs/third_algorithm_experiment_log.md` 참고. 이 보드는 라이브 인덱스.*
