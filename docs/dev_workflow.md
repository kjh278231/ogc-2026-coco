# 개발 워크플로 결정 근거 (single session vs agent/skill화)

## 결정

- **진단·종합 루프는 단일 세션으로 유지한다** (비동기 배치 + memory 체크포인트 병용).
- **방법론은 skill화한다** (맥락을 깨지 않고 규율화 — 순이득).
- **병렬 가능한 기계적 작업만 agent화한다** (활용기에, throughput이 compute-bound일 때).
- **연속성의 단위는 세션이 아니라 memory다** (비대해지면 distill → fresh로 재수화).

## 결정적 전제

제출 solver는 **sandbox에서 LLM 없이 / 인터넷 없이** 실행된다(문제지 §3.2). 따라서 **agent/skill은
채점 대상(`myalgorithm.py`)의 일부가 될 수 없다.** 이 질문은 "점수 산출물을 어떻게 만들까"가 아니라
**"더 좋은 solver+보고서를 더 빨리 만드는 개발 과정"**의 문제다. 점수 = solver 품질 + 기술보고서/발표.

## 근거

1. **점수 견인은 compute가 아니라 insight였다.** 이번 돌파(disjoint packing으로 crane 제거, 병목이
   ENTRY)는 전부 *실험 간 종합*의 산물이다. Exp A는 Exp 0/2가, 프로토타입의 trap→disjoint 전환은
   Exp 0이 맥락에 있어야 성립했다. **"실패를 구조 신호로" 루프는 전체 진단 이력을 요구한다.** cold
   agent로 쪼개면 맥락 재유도 비용이 들고, *정작 돌파를 만든 cross-experiment 종합이 경계에서
   잘린다.*
2. **보고서 점수도 일관 서사의 산물**이다(진단→설계→검증). 분절은 서사 조립을 어렵게 한다.
3. **agent가 이득인 곳은 병렬 기계적 fan-out뿐**이다(전 20개 배치, 파라미터 스윕, multi-restart).
   그러나 이는 이미 background 비동기로 처리 중이며, 별도 agent는 *서로 독립인 여러 config를 동시
   탐색*해야 하는 활용기에만 추가 이득이 있다.
4. **skill은 별도 컨텍스트가 아니라 현재 세션 안의 절차**라 연속성을 보존하며 방법을 규율화한다 —
   순이득.
5. **긴 세션의 비용/요약 위험**은 실재하지만, 해법은 agent 분절이 아니라 **memory 체크포인트 →
   fresh 세션 재수화**다. 종합을 보존한 채 깨끗한 컨텍스트를 얻는다.

## 단계 의존

| 단계 | 권장 | 이유 |
|---|---|---|
| 발견·진단 (현재) | 단일 세션 + 비동기 배치 + memory | insight=종합=점수 |
| 활용·스케일 (프레임워크 확정 후) | 기계적 병렬=agent, 방법론=skill | compute-bound 전환점 |
| 제출물 | 해당 없음 | solver는 sandbox, LLM 불가 |

## 도입한 skill

`.claude/skills/` 에 방법론 3종을 codify했다 (방법론 전체는 `docs/methodology.md`):
- **cheap-falsification** — 가설을 가장 싼 실험으로 죽인다 (build 전).
- **oracle-validate** — 자체지표 금지, `check_feasibility`로 전 instance 검증 + 비회귀 가드.
- **bug-or-finding** — 극단적 결과는 불가능성 논증으로 검산 후에만 채택.
