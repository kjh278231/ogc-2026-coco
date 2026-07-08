# 신규 솔버 설계 — HELM: 인스턴스-적응 앵커 스펙트럼 라우팅

> 사용자 목표(2026-07-03): 지금까지 실험을 종합해 **가장 기대 성능이 높은 엔진**을 개발하고
> 지속 개선. 이 문서는 6번째 엔진 HELM의 근거·설계·검증 게이트를 담는다.

## 0. 근거 — 왜 라우터인가 (세션 누적 메타결론)

WEAVE 세션의 ⭐메타결론: **모든 메커니즘이 instance-split**이다 — ejection/PR/hybrid/
guided-destroy/MIP-seed 전부 일부 인스턴스 승·일부 패, 4코어 포트폴리오가 트레이드오프를
강제한다. PRISM+MO는 잘 튜닝된 최적점, WEAVE/FLUX는 경쟁적 대안점이고, **단일 메커니즘
추가로는 strict 지배가 불가능**했다. "진짜 이득 = 인스턴스별 best 솔버 선택"이 결론이었는데,
이것이 아직 **엔진으로 구현된 적이 없다**. HELM이 그 구현이다.

측정된 승자 지도 (PRISM+MO 기준, eval-count+wall 종합):
| regime | 승자 | 근거 |
|---|---|---|
| 혼잡-schedulable (T5/T11/T13/T14) | **FLUX** (−9~18%) | 혼잡앵커가 Z1=0 basin 직행 |
| 저혼잡/packing-sensitive (T4) | **PRISM** (FLUX +5.9% 패) | a_pref-basin 재시작 다양성 필요 |
| irreducible Z1>0 (T38, P6류) | **PRISM** (FLUX +3.6% 패) | Z1 물리하한, 혼잡앵커 무력 |
| tie (T2/T20) | 동률 | 양쪽 다 Z1=0 도달, mip16이 Z2 우세 |

## 1. 라우팅 신호 (cheap-falsification ✅ 통과, `_router_features.py`)

패커 없이 <0.5s에 계산되는 2신호가 승자 그룹을 깨끗이 분리한다 (train 40개 전수):
- **max_util** = a_pref에서 bay별 peak demand-window footprint-면적 이용률의 최댓값
  (FLUX의 검증된 혼잡 신호). FLUX-승 그룹 = 1.61~2.33, T4 = 1.07, T1 = 0.98.
- **rho** = energetic floor: mandatory-part(어떤 스케줄에도 반드시 존재) 면적 peak / 총
  용량. rho>1 = Z1 물리하한 (T38=1.76; train 12개가 이 부류 = P6존).

규칙(v1): `rho > 1.0 → IRREDUCIBLE`, `max_util ≤ 1.2 → LOW`, `max_util ≥ 2.5 → HEAVY`,
그 외 `CONGESTED`. 경계 사례는 T1(0.98, FLUX가 −2.3%로 승) 하나뿐 — LOW로 보내도 손실 최소.

**HEAVY 밴드 추가 근거(07-03 wall 진단)**: T20@180에서 혼잡 스펙트럼이 세 번의 독립 draw
(기본/+ejection/greedy→capped) 모두 Z1=1에 고착(140020~140095, 챔피언 대비 +23%) — 전 워커가
마지막 tardy 1을 못 지우는 나쁜 복권. 반면 챔피언 config(PRISM 스펙트럼+seed 20260629)는
Z1=0 (113657) 도달. heavy-schedulable(=P4/P5존)은 챔피언 라우팅 = 결정론적 동등성.
유일한 측정된 희생 = T19의 eval-only −8.7% upside (wall서는 T6/T18처럼 소멸 가능성).

## 2. 설계

```
classify(prob) → regime → 앵커 스펙트럼 (regime별 측정 승자의 config 그대로)
  LOW/IRREDUCIBLE → {pref, balanced, capped, mip16}   (=PRISM config D, seed 20260629, mip_tl 4)
  CONGESTED       → {apref, greedy0.9, mip16, mipcong} (=FLUX,        seed 20260702, mip_tl 6)
→ 공유 포트폴리오 하니스 (master 앵커 1회/Gurobi 직렬 + 워커 NORECOMB LAHC+swap+ILS
  + union-recombine + best-of + guarded idle-reclaim)  ← prism/flux portfolio와 동일
```

- 신규 코드는 **라우터뿐**: 앵커 생성기·정제 커널·포트폴리오는 전부 검증된 것 재사용
  (flux_engine이 mip16/mipcong/greedy 전부 보유; 휴리스틱 trio는 커널 K의 함수).
- regime별 seed/mip_tl까지 원 엔진과 일치 → **serial 경로는 원 엔진과 bit-identical 재현**
  가능 = 검증 게이트가 명확.
- `HELM_FORCE_REGIME`으로 라우팅 강제 (A/B·비상 롤백), `HELM_UTIL_HI`/`HELM_RHO_HI` 경계 튜닝,
  `HELM_ANCHORS_<REGIME>`으로 스펙트럼 오버라이드.

## 3. 기대 이득 (P1~P6 매핑)

- P3존(Z3-heavy, 180~300s) 및 schedulable-heavy → CONGESTED 라우팅 = FLUX의 broad 승
  (T13 −12% wall, T11 −18% eval) 획득.
- P1/P2(소형·저예산) → 대부분 LOW = 챔피언(PRISM+MO) 동작 보존 (T4형 회귀 차단).
- P6(irreducible) → IRREDUCIBLE = 챔피언 보존 (FLUX의 T38 +3.6% 회귀 차단).
- 즉 **기대값 = per-instance max(PRISM+MO, FLUX)** − 라우팅 오류 리스크.

## 4. 검증 게이트 ([[eval-count-ab-protocol]] [[anchor-to-grader-best]])

1. ✅ 라우터 전제 falsification (위 §1).
2. **재현 게이트**: serial eval E=2500에서 HELM==PRISM (T4/T38), HELM==FLUX (T13/T11).
3. **미검증 밴드**: CONGESTED로 라우팅되는 미측정 인스턴스(T6/T7/T10/T12/T15/T16/T17/T18/T19)
   PRISM vs FLUX eval A/B → 회귀 발견 시 경계/스펙트럼 조정.
4. wall A/B (T=180/300, 포트폴리오, close-in-time) vs PRISM+MO.
5. flat zip + 추출 스모크 (feasible·overrun無) → 그래더 제출(user).

## 5. 채택된 개선 (v1)

- **IRREDUCIBLE → MO-off** (`_route_env`, `HELM_IRRED_MO` 게이트): Z1이 물리하한이면 MO의
  tardy-bay best-of는 이득 없이 eval당 2~3× 비용만 소모(T38 eval당 ~4.7s). wall T38@300:
  **MO-off 63884676 vs 챔피언 PRISM+MO 67166362 = −4.9% 승** (MO-off 4워커 전원이 MO-on
  최강워커보다 우수). 그래더 독립 증거: 0629 PRISM(=MO 이전)이 P6 best 보유.
  **일반화 검증(07-03, `_helm_t37_moff.txt`)**: T37@300 = 5727901 vs 챔피언 5725512
  (+0.04% 동률; Z1은 1365<1444 우수) → T38 승 + T37 무해 = 회귀 없음, 채택 확정.
- **HEAVY 밴드** (위 §1): T20-class 챔피언 라우팅.

## 6. 개선 백로그 (지속 개선용)

- 경계 튜닝: UTIL_HI 1.2의 민감도 (T3 1.05/T9 1.07이 LOW 쪽 — 맞는지 eval로 확인),
  UTIL_HEAVY 2.5 (T14 2.33 CONGESTED-승 vs T20 2.82 HEAVY 사이).
- ~~T19(3.22, HEAVY로 이동) wall A/B~~ ✅완료(07-03): heavy=congested=60746 정확 동률
  (`_helm_t19_boundary.txt`) — eval −8.7% upside는 wall서 소멸, HEAVY 경계 비용 0 확정.
- IRREDUCIBLE 전용 스펙트럼: mipcong의 overflow-최소화 lex가 Z1 하한 근접+Z3 균형에
  도움되는지 (FLUX 설계의 미검증 가설; T38/T37/T39 wall로 판정). + MO-off와 결합.
- WEAVE population을 LOW-대형·장예산 sub-regime에 (T1 −71% 발견 = a_pref 트랩 탈출).
- 라우팅 오류 안전망: 포트폴리오 4워커 중 1개를 타 regime 앵커로 (best-of가 흡수).
- 진단 개선: portfolio LAST의 worker_tot↔anchor_names 라벨이 완료순 vs 제출순 불일치
  (전 솔버 공통) — 결과에 앵커명 동봉하도록 수정하면 향후 진단 신뢰도↑.
