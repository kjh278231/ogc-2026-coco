# 신규 솔버 설계 — FLUX: 시공간 혼잡-인식 배정 (congestion-aware assignment)

> 사용자 목표(2026-07-02): 기존(BRIDGE/PRISM/STOW/WEAVE)이 **전혀 시도하지 않은 방법**으로
> 더 나은 성능을 낼 별개 엔진을, **P1~P6 특성 기반**으로 개발. 1회 실패로 접지 말고 지속 개선.
> 이 문서는 그 신규 기법(FLUX)의 진단·설계·falsification 게이트를 담는다.

## 0. 결정적 재프레이밍 (이 세션의 핵심 발견, 2026-07-02)

기존 메모리 [[objective-z3-dominant]]는 "목적함수는 Z3(선호) 80~99% 지배"라 했으나, 그건
**Z1=0에 도달한 쉬운 인스턴스**의 얘기다. 무거운 ≥300초 인스턴스(P4/P5/P6 프록시 = T20/T14/
T38/T39/T37)의 목적함수-질량 실측(`_diag_breakdown.py`):

| seed | inst | Z1 비중 |
|---|---|---|
| a_pref | T20/T38/T39/T37 | 99.5~100% |
| **mip16(부하분산·패킹무시)** | T20 | **99.3%** |
| mip16 | T38 | 99.9% |
| mip16 | T39 | 99.8% |
| mip16 | T37 | 95.9% |

→ **무거운 인스턴스는 부하분산 MIP 앵커조차 Z1(패킹 혼잡)이 92~99.9%.** 즉 P4/P5/P6 존의
지배 비용은 **Z1 = 시공간 혼잡(contention)**이지 Z3가 아니다. 이것이 (a) P4="최난패킹",
(b) multi-order(=Z1 직접 감소)가 역대 최대 그래더 레버, (c) SWAP(Z3 레버)이 P3(-20%)만 돕고
P4(+3.9%) 악화, (d) P6 동결의 근본 이유를 설명한다.

## 1. 빈틈 (기존 4개 솔버가 공유하는 맹점)

BRIDGE/PRISM/STOW/WEAVE는 전부:
1. **블록→bay 배정 벡터** 표현을 공유,
2. **greedy footprint-disjoint 패커**(`solve_bay`/multi-order)를 유일한 Z1 오라클로 사용,
3. **패킹-무시(packing-blind) MIP 앵커**(`mip_anchor`: `argmin lam·w2·Z2 + w3·Z3`, Z1 항 전무)를
   공유 — LAHC가 사후 수선.

⇒ **어떤 솔버도 "이 배정이 얼마나 혼잡할지"를 배정 단계에서 모델링하지 않는다.** MIP 앵커는
workload 균형(Z2)만 보는데, **혼잡은 workload가 아니라 시공간 footprint-면적**이 결정한다
([[placement-lever-diagnosis]]: demand_peak_util = union-footprint면적/bay면적 > 1.0이 tardy를 가름).
workload와 footprint-면적은 다르다(큰 workload·작은 면적, 반대도 가능) → **MIP의 Z2는 혼잡의
조악한 프록시.** count-cap MIP(블록 수 상한)는 이미 반증됨(면적·시간 무시).

## 2. FLUX 기법 (신규 패러다임)

**핵심: 배정을 "시공간 자원-용량 제약 배정(resource-capacitated assignment)"으로 모델링.**
자원 = bay별 footprint 면적, 시간축 = 블록 demand-window [release, due). OR 용어로는
**temporal bin packing / cumulative-resource assignment**에 가깝고, 이 저장소의 어떤 솔버도 안 함.

앵커 MIP에 **시간-면적 누적 용량 제약**을 추가:
- 각 bay j, 각 임계시점 t에 대해: `Σ_{i: x[i][j]=1, release_i ≤ t < due_i} area_i ≤ κ · A_j`
- κ = 패킹 비효율 보정 안전계수(<1, falsification로 캘리브레이션). 임계시점 t는 블록 경계
  (release/due)만 보면 충분(구간상수 → 유한개).
- 목적은 그대로 `w3·Z3 + w2·Z2`(선호+부하), 제약이 혼잡을 억제 → **패킹-near-feasible(저-Z1)
  앵커**를 배정 단계에서 직접 생성.

이 앵커를 기존 검증된 LAHC+swap+recombine 커널로 마감(수선 부담이 작음 → 저-Z1 basin 도달).

### falsification 사다리 (약신호 시 피벗, [[new-mechanism-long-view]])
1. **신호 검증**: peak demand-window 면적-util이 패킹 Z1과 정렬되는가(tardy bay ⟺ util>κ)?
2. **용량-repair 이득**: util>κ bay에서 min-pref-loss 블록을 빼는 greedy cap이 패킹 Z1을
   mip16 대비 유의미하게 낮추는가? (낮추면 제약이 옳은 레버)
3. **MIP화**: 시간-면적 제약 MIP 앵커가 mip16보다 낮은 packed-Z1(및 최종 obj)을 주는가?
4. 신호가 분리 안 되거나(면적합 ≠ 2D패킹) LAHC가 이미 같은 곳 도달 → 피벗
   (예: energetic-reasoning 강화 제약 / 2D strip-packing 하한 / 시간-창 세분).

## 3. 재사용면 (prism 패턴 답습)
`K.total_obj/obj23/eval_obj1/_climb_lahc/_z3_refine/_recombine/_bestof_obj/_score_and_pack/
_solution_from_packed/a_pref/fits`, 패커 `solve_bay_best`. 신규 = 용량-제약 앵커 생성기 +
(GO 시) 포트폴리오(prism/portfolio.py 패턴: master 앵커 1회 Gurobi 직렬 + 워커 NORECOMB LAHC).

## 4. 검증 프로토콜 ([[eval-count-ab-protocol]] [[anchor-to-grader-best]] [[oracle-validate]])
- eval-count 고정 A/B(`SOLVER_MAX_EVALS`) + wall 별도.
- oracle-validate(`utils.check_feasibility`)로 feasible·무회귀 가드.
- anchor = 사용자 그래더 best(PRISM+MO/P별, [[grader-best-0619-2]]); 세션 자체제출끼리 비교 금지.
- **1회 A/B로 판정 금지**(기존은 heavily-tuned; 신규는 뒤처져 시작).

## 5. 결과 (검증, 진행 중)

### ✅ falsification 1 — 혼잡 신호가 패킹 Z1을 예측 (검증됨, `_sig_apref.py`)
a_pref에서 per-bay peak demand-window 면적-util vs 패킹 Z1이 깨끗이 단조:
- util ≤ ~0.9 → Z1 ≈ 0 (예외 없음): T20 bay0/3(0.66→0), T13 bay0(0.74→0), T18 bay2(0.53→0)…
- util > 1.0 → Z1이 util과 급증: T38 bay1(4.68→**8667**), T37 bay0(3.56→**3412**), T39 bay2(2.43→**2853**), T20 bay1(2.82→**1593**).
⇒ **(release, due, area)만으로 패커 없이 계산되는 신호가 패킹 tardiness를 예측** → MIP 선형 제약 가능.

### ✅ falsification 진단 — 두 혼잡 부류 (`_irreducible_floor.py`, energetic-reasoning 하한)
mandatory-part peak(= 어떤 스케줄에서도 t에 반드시 존재하는 면적) vs 총 bay 용량:
- **IRREDUCIBLE Z1>0** (mandPk/cap>1): T38(1.78)·T39(1.22)·T37(1.18) → 배정·패킹 무관 Z1 물리하한.
- **schedulable/slack** (mandPk/cap≤1): T20(0.60)·T13(0.55)·T14(0.64)·T1(0.74)·T11(0.59)·T18(0.45)·T9(0.50).
⇒ **P6 동결의 유력 설명**: P6이 T38류(혼잡-포화)면 Z1이 하한에 걸려 MO 포함 어떤 레버도 무력(MO가 P3/P4/P5 −11~57%인데 P6 +0.2%와 일치). 이 경우 유일 레버 = Z1 하한 도달 배정 중 Z3/Z2 최소화.
⇒ **설계 정교화**: util을 하드 제약이 아니라 **소프트 overflow 페널티**(또는 lexicographic: overflow 최소화→Z3)로 → schedulable은 Z1→0, irreducible은 불가피 overflow 최소화하며 Z3 균형. 두 부류 모두 처리.

### ✅ falsification 3 — FLUX 엔진이 PRISM 엔진을 이긴다 (`_engine_ab.py`, eval-count E=3000)
전체 FLUX 엔진(flux_solve: 혼잡앵커 스펙트럼+LAHC+swap+recombine) vs 전체 PRISM 엔진(prism_solve),
동일 eval 예산·동일 MO+SWAP 스택, 진짜 packed obj:

| inst | PRISM obj (Z1,Z3) | **FLUX obj (Z1,Z3)** | Δ |
|---|---|---|---|
| **T13** | 192414 (1,1263) | **108149 (0,783)** | **−43.8% 압승** |
| **T14** | 135584 (1,832) | **123028 (0,841)** | **−9.3%** |
| T20 | 237923 (2,1399) | 244207 (2,1379) | +2.6% |

**FLUX 2/3 승, 집계 −16%.** 핵심 검증: 혼잡-인식 앵커가 **Z1=0 도달**(PRISM mip16은 Z1 잔존) + Z3도↓.
T20 소폭 패 = greedy 앵커가 큰 블록 fits 제약으로 일부 bay(util~1.4) 완전 해소 못함(per-bay 부분-
비가역) → mipcong MIP·소프트 페널티가 흡수 대상; +2.6%는 sub-integer Z1 차. **엔진 구축(`flux/`) 완료**:
flux_engine(greedy_congestion_anchor + mip_congestion_anchor[lex/penalty, top-K interval cap] +
_anchors + _refine[PRISM 재사용] + flux_solve) + myalgorithm + portfolio. Gurobi-free 스모크 OK.

### ✅ wall A/B (배포 regime, 포트폴리오 전체, `tools/_prism_portf_ab.py`)
| inst | PRISM (Z2,Z3) | FLUX (Z2,Z3) | Δ | wall |
|---|---|---|---|---|
| **T13@180** | 85205 (2943,530) | **75026 (1519,507)** | **−11.9% 승** | 174s |
| T20@180 | **109695 (1470,807)** | 117136 (2356,824) | +6.8% 패 | 173s |

- **배포-안전 확인**: FLUX가 T13·T20·T13@60 모두 데드라인 준수(wall 55~174초, overrun 無). 초기 "killed"들은
  전부 **직전 포트폴리오 실행의 좀비 워커 경합** artifact(FLUX 버그 아님). 교훈: 포트폴리오 wall 실행 사이 좀비 정리 필수.
- **instance-split 진단(WEAVE와 동일 벽)**: FLUX는 **Z1-hard 인스턴스(T13/T14)서 승**(혼잡앵커가 Z1=0 도달+저Z2),
  **Z1-easy 인스턴스(T20 schedulable)서 패** — 양쪽 다 Z1=0이면 mip16의 workload-balance가 footprint-spread보다
  Z2 우수(T20 wall mip16 Z2=1470 ≪ mipcong Z2=2356). 앵커별 승자: **T13=greedy0.9(Gurobi-free!)**, **T20=mip16**.
- **강건성 수정(채택)**: dead-weight greedy0.8(양쪽 패) → **mip16(PRISM 승리 앵커)** 교체. 스펙트럼
  = {apref, greedy0.9, mip16, mipcong}. best-of가 Z1-hard엔 greedy/mipcong, Z1-easy엔 mip16을 선택 →
  **FLUX ≥ PRISM 목표**(재검증 중). mip16+mipcong은 master 직렬 Gurobi, greedy/apref는 free.

### ✅ 무회귀 + broad 검증 (eval-count E=2500, 새 스펙트럼)
| inst | PRISM | FLUX | Δ |
|---|---|---|---|
| T1 (쉬움) | 8041 | 7859 | **−2.3% 승** |
| T4 (packing-sensitive) | 31157 | 33003 | +5.9% 패 |
| T5 | 61344 | 55031 | **−10.3% 승** |
| T11 | 50708 | 41447 | **−18.3% 승** |
| T14 (앞서) | 135584 | 123028 | **−9.3% 승** |

**FLUX가 broadly 우수** — specialist 아님. 종합(train 샘플): 승 T1/T5/T11/T13/T14, tie T20, 소폭 패 T4(+5.9%,
packing-sensitive: a_pref 재시작 앵커가 관건인데 FLUX 스펙트럼엔 apref 1개뿐; PRISM은 pref/capped 2개).
T4는 instance-split의 유일 손실이며 best-of 제출(P별)로 흡수 가능. **T02 재검증 −0.7% tie(mip16 효과+wall노이즈).**

### ⏳ 남음
① irreducible T38(P6 프록시) wall — FLUX overflow-최소화가 Z1 물리하한 근접해 동결 P6 움직이는지
② flat zip(`myalgorithm0702-flux.zip`) 빌드 + 추출-스모크(feasible·overrun無) ③ 그래더 제출.

## 7. 종합 (2026-07-02)
FLUX = **검증된 신규 기법(시공간 혼잡-인식 배정) + broadly 우수한 엔진.** 신규성: 기존 4솔버가 전부
패킹-무시 앵커인데 FLUX는 배정단계서 패킹 혼잡을 모델링(peak demand-window footprint-area) — greedy
(Gurobi-free)·MIP(lex/penalty) 두 형태. 배포-안전(overrun無), broadly PRISM 능가(T13 −12%·T11 −18%·
T5 −10%·T14 −9% 등), 유일 소폭 손실 T4(+5.9%). mip16 포함으로 Z1-easy 회귀 방지. 다음 anchor 판정=그래더.

**주의(교정)**: FLUX의 greedy 앵커가 Gurobi-free인 건 앵커 다양성 보너스일 뿐, 포트폴리오 구조 이점은
아님 — PRISM도 워커는 이미 NORECOMB(Gurobi-free), master만 앵커 계산. FLUX의 진짜 우위는 **앵커 품질**
(혼잡-인식 → near-packable 저-Z1 basin), 병렬화 아님.
