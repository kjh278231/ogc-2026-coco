# 신규 솔버 설계 — 배정벡터 population/recombination 계열 (작업명 잠정)

> 사용자 목표(2026-07-02): PRISM/BRIDGE 외 **완전히 새로운 기법**을 신규 폴더에서 테스트하고,
> 1회 실패로 접지 말고 지속 개선. 이 문서는 그 신규 기법 계열의 설계·검증 게이트를 담는다.

## 1. 문제 구조 (왜 이 기법인가)

`check_feasibility`/`total_obj` 분석(bridge/utils.py:1406, bridge/solver.py:96) 확정:

- **obj3 (선호 페널티) = Σ_i (max_pref_i − pref_i[assigned_bay])** — **순수 배정벡터 함수**.
- **obj2 (부하 불균형) = floor(max_pairs |u_a·load_a − u_b·load_b|)**, load_j = Σ workload — **순수 배정벡터 함수**.
- **obj1 (지연) = Σ max(0, exit_i − due_i)** — 패킹/스케줄 의존, 17/20서 0.
- 가중치 `w1 ≫ w3 ≫ w2` (예: T1 29091/200/7) → **near-lexicographic**: 먼저 Z1→0, 그다음 Z3, 마지막 Z2.

⇒ 본질은 **N블록을 M bay로 분할(partition)해 `w2·Z2 + w3·Z3`(순수 배정비용)를 최소화, 각 bay 블록집합이 패킹+크레인 가능·저지연이어야 함.** 지배 레버는 **분할 그 자체**.

## 2. 빈틈 (기존 기법이 못 하는 것)

- **BRIDGE**: 단일궤적 ILS/LAHC(relocate) + swap + MIP-repair + column-recombine + best-of 포트폴리오.
- **PRISM**: MIP 앵커 스펙트럼 → 각 앵커를 **독립** LAHC 정제 → best-of + column-recombine. 앵커 간 정보교환은 **최종 recombine(bay-column 레벨)뿐**.
- **STOW**: 패킹정책 다양성 포트폴리오.
- ⇒ **탐색 도중 배정벡터 수준에서 구조를 교환하는 공진화 population은 전무.** column-recombine는 *발견된 조각을 선택*할 뿐 두 부모의 부분분할을 *합성*하지 못함(relocation/swap은 1블록 국소).

## 3. 기법 계열 + falsification 게이트

**핵심 전제**: 엘리트 배정벡터의 재조합(crossover/path-relinking)이 **동일 eval예산 ILS(최고엘리트 연장)**보다 낮은 feasible obj에 도달.

- 프로브 `.claude/scratch/_xover_probe.py` (eval-count 결정론): 엘리트풀(pref/bal/cap/mip1/mip16 LAHC수렴) → uniform/greedy crossover + 패킹수선 + LAHC polish vs 동일예산 ILS. 무료신호=Hamming 다양성·oracle-mix Z3 하한.
- **연산자 사다리 (약신호 시 피벗, [[new-mechanism-long-view]])**:
  1. **uniform/greedy crossover** (프로브 기본).
  2. **path-relinking**: 부모 A→B 경로를 1블록씩 재배정하며 모든 waypoint를 `_bestof_obj`로 평가·best 유지(=greedy child의 완전판; 국소 crowding을 우회).
  3. **scatter search**: 참조집합(품질+다양성) + 구조적 조합 + LAHC 정제.
  4. **Tabu search**: 재조합이 전부 죽으면, adaptive-memory 국소탐색(LAHC와 다른 basin 탈출축)으로 신규성 확보.

## 4. 아키텍처 (GO 시)

재사용면(전부 bridge/solver.py + prism/prism_engine.py, import는 prism 패턴 답습):
`K.total_obj / obj23 / _climb_lahc / _perturb / _z3_refine / _recombine / _bestof_obj(Pareto-safe 가드) / _score_and_pack / _solution_from_packed / build_solution / a_pref·a_balanced_load·a_pref_capped`, `P.mip_anchor / _anchors`.

- **population**: K개 엘리트(배정 dict + obj). 초기화 = 앵커(pref/bal/cap/**mip 스펙트럼 필수** — 풀 다양성의 원천, MIP 앵커 없으면 pref≈cap로 퇴화) 각 LAHC정제 + perturb 다양화.
- **부모 페어링(측정된 설계 결정, `_recomb_probe2` T18)**: **최고 × 최다양(max-Hamming) 파트너**가 최고×차선(top-2-by-obj)보다 우월 — T18서 cap(76549)×bal(다양) uniform crossover+polish=**70594(−7.8%)로 동일예산 ILS(76549 무개선)를 이김**, 반면 top-2-by-obj 페어링(run 1)은 −0~+2.8%. 즉 **재조합 재료 = 배정 다양성**이지 부모 품질이 아님.
- **세대 루프(deadline까지)**: 부모2 선택(최고×다양) → 재조합(uniform가 greedy·pr_fixed보다 나음, T18) → LAHC polish(+swap) → **`_bestof_obj` 가드로만 채택(monotonic 회귀불가)** → crowding 삽입(Hamming 다양성 유지).
- ⚠ greedy crossover(블록별 Z3-min)는 **과적 붕괴**(T18 +25%): Z3만 좇아 선호 bay 몰림 → Z1. uniform(확률적 혼합)이 다양성·feasibility 균형에서 우월.
- **주기적 path-relinking**(엘리트쌍) 강화.
- **최종**: union `_POOL` column-recombine(PRISM식) → best materialize.
- **병렬**: spawn-guard(prism/portfolio.py 패턴) 답습. population은 **island 모델**(4 island 소population + 주기적 migration)이 자연스러움. T≥게이트에서만 병렬, 미만은 단일 population.

## 5. 검증 프로토콜 (memory 규칙)

- **eval-count 고정 A/B**(`SOLVER_MAX_EVALS`, 결정론) + wall 별도. [[eval-count-ab-protocol]]
- **oracle-validate**: `utils.check_feasibility`로 feasible·회귀가드. 절대 자체계산 obj 신뢰 금지.
- **anchor = 사용자 그래더 best(PRISM+MO 0701)**, 세션 자체제출끼리 비교 금지. [[anchor-to-grader-best]]
- 러너 `tools/_prism_portf_ab.py`에 `elif algo=="<name>"` 편입 → wall T=180/300 A/B.
- **1회 A/B로 판정 금지**(BRIDGE는 heavily-tuned; 신규는 뒤처져 시작) — 장기 지평. [[new-mechanism-long-view]]

## 7. 결과 (검증, 2026-07-02)

- **cheap-falsification (`_recomb_probe2`, eval-count)**: path_relink이 동일예산 ILS를 하드 3/3 압승(T18 −7.8·T13 −22.6·T11 −32.6%). blind uniform은 고분산(T13 partner Hamming 0.456서 +33.9% 붕괴=manifold 이탈). → **PR 주력, uniform은 ablation.**
- **서열 eval A/B (E4000, WEAVE vs PRISM serial, 진짜 grader obj) 3/3 압승**: T13 90715 vs 163062(−44%)·T11 29694 vs 36274(−18%)·T18 55328 vs 90316(−39%). WEAVE가 ejection-chain로 **Z1=0 도달**(PRISM은 빡빡한 예산서 Z1=1~2 잔존).
- **🔥 T1 finding**: "결정적 트랩"(a_pref 15611)이 실은 −71% 개선 여지 보유 — WEAVE-pf T=90서 **4539**(grader feasible). proxy==build==grader 일치(bug-or-finding 검증).
- **wall A/B (T=180, WEAVE-pf vs PRISM+MO-pf, 배포 regime)**: T13 **81573 vs 85205 = −4.3% WIN**(Z3 476 vs 530). 서열 −44%보다 작음(PRISM 4코어 포트폴리오가 격차 축소)이나 rank-based서 유효. T18/T20 진행 중.
- **부산물**: ejection-chain 이동은 relocation+swap이 못 가는 Z1=0 도달을 값싸게 달성 → **PRISM/BRIDGE에도 이식 가능한 범용 이동**(SOLVER_EJECTION 후보, 병렬 트랙).

## 6. 리스크

- ⚠ column-level union이 과거 실패([[portfolio-recombine-degraded]])했으나 그건 강탐색 incumbent 민감성; 배정벡터+fresh repair는 다른 연산자(프로브가 판정).
- 엘리트가 동일 basin으로 수렴(다양성 붕괴)하면 재조합 무력 → 다양성 유지 필수(프로브 Hamming으로 사전 진단).
- throughput: crossover child는 미캐시 bay집합 다수 → 초기 eval 비쌈([[placement-lever-diagnosis]]의 multiorder throughput 교훈). island/best-of로 흡수.
