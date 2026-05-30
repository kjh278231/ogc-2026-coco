# OGC 2026 — Improvement Log

Hermes solver (`baseline/myalgorithm.py`)에 대한 승인된 개선 사항 로그.
최신 항목이 위. eval → analyst → strategist → developer → re-eval → approval-gate
파이프라인이 APPROVE 또는 REVIEW-lean-approve 판정에 도달하고 변경이 머지된 뒤에만
항목이 추가된다.

각 항목의 스키마:
- **Verdict** — APPROVE / REVIEW-lean-approve (단서 포함)
- **Hypothesis** — 한 줄 thesis
- **Change locus** — patch가 건드린 파일과 함수
- **Baseline vs target** — run_id 비교, 핵심 metric
- **Commit** — `feature/my-algorithm` 위의 short SHA
- **Caveats / follow-ups** — 미해결 risk, 보류된 작업

---

## H-001 — edd_retry 폴백 건너뛰고 forced placement로 직진

- **머지일**: 2026-05-30
- **Verdict**: REVIEW (lean-approve) — bench mean ratio 0.857, regression 0건, 두 soft fail 모두 절차적
- **Hypothesis**: 4개 init heuristic(EDD, SlackRatio, MST, LargestArea)이 모두 hard bench_B5 instance에서 stage 2 (crane feasibility) 실패할 때, `edd_retry` 폴백이 같은 EDD를 budget만 늘려 재실행하느라 ~20초를 진행 없이 소모. `edd_retry`를 건너뛰고 forced placement로 곧장 가면 simulated annealing에 ~18초의 wall-clock이 회복된다.
- **Change locus**: `baseline/myalgorithm.py` — `algorithm()` 안의 `if best_perm is None:` 블록 (이전 608–625 라인). 이전에 commit되지 않았던 인프라(JSONL event-log `_emit`, OBB local-poly cache, `baseline_greedy.py`의 `_ACTIVE_DEADLINE` 전파, SA loop 리팩토링 `time_budget`→`search_deadline`, `tight_blocks` precompute 호이스팅)도 함께 묶임.
- **Baseline (run_2) vs target (run_3)** — pattern=`bench_B5_*.json`, timelimit=30s:

  | Instance | obj before | obj after | Δ% | sa_iters before | sa_iters after | sa_improvements |
  |---|---:|---:|---:|---:|---:|---:|
  | bench_B5_b120_preference_skew | 14,325,851 | 10,216,451 | **−28.7%** | 0 | 9 | 3 |
  | bench_B5_b150_mixed_hard | 32,619,533 | 32,619,533 | 0.0% | 0 | 9 | 0 |

  Bench mean obj ratio: **0.857**. Feasibility 유지(둘 다 stage 5 통과). Fallback 트리거는 여전히 발생하지만 `edd_retry`→`forced` 대신 `forced_direct`로 바뀜.

- **Commit**: `4972d5f` on `feature/my-algorithm`
- **Caveats / follow-ups**:
  - Smoke instance는 H-001 코드로 **재실행하지 않음** (R2 절차적 soft fail). Architectural argument: `edd_retry` 경로는 모든 seed가 stage 2 실패할 때만 발화하고 smoke에서는 발화하지 않음. 데이터로 확인은 보류.
  - R4 SA-throughput 규칙은 baseline `sa_iterations=0`이라는 병리(=H-001이 해결하려던 바로 그 문제)에서 트리거됨. 방향은 strictly positive (0 → 9).
  - **b150은 그대로**: SA의 swap/insert/invert neighborhood가 이 instance에서 forced placement local optimum을 못 벗어남. 다음 가설 후보: 더 거친 neighborhood (다른 bay로의 block 재할당) **또는** H-002 (SA 시작 전에 `repair_mode='simple'` seed를 끼워 넣어 non-degenerate 시작점 제공).
  - solver-developer 에이전트가 엄밀한 `best_perm is None` 블록 바깥까지 손댐(SA loop 리팩토링, dead-code 제거). 동작은 eval로 검증됨; hygiene 차원에서 향후 참고용으로 표시.

---
