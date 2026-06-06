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

## Codex 진행 중 기록

### M-001 — eval/approval 에이전트 최신화

- **날짜**: 2026-06-02
- **상태**: 적용 완료. Solver 코드는 변경하지 않음.
- **Change locus**:
  - `.claude/agents/eval-analyst.md`: baseline 선택 시 same algo뿐 아니라 same runner config, same instance set을 우선하도록 명시. `prob_*` full regression과 targeted probe를 섞지 않도록 규칙 추가.
  - `.claude/agents/approval-gate.md`: canonical `training_set/prob_1`..`prob_20` full-suite gate 추가. aggregate 0.1% 초과 회귀, 5% 초과 단일 instance 회귀, 회귀 count가 개선 count보다 많은 경우를 rule로 분리.
  - `.claude/agents/README.md`: full regression과 targeted probe를 섞지 않는 공통 규약 추가.
  - `.claude/scratch/*.jsonl`: Codex-H006 승인, Codex-H007/H008 롤백 이력을 로컬 에이전트 메모리에 추가. 이 디렉터리는 git ignore 대상이므로 repo diff에는 표시되지 않는다.
- **배경**: 기존 prompt는 Athena/parallel/event 해석은 반영되어 있었지만, `prob_*` full regression과 targeted probe run을 분리하는 규칙이 약했다. H-006 분석에서 `eval_summary --baseline-window`가 targeted probe를 baseline pool에 섞는 문제가 실제로 발생했으므로 이를 명시적으로 차단했다.

### H-008 — `place_initial()` tardy release rescue

- **날짜**: 2026-06-02
- **상태**: **실험 후 롤백됨**. Targeted `prob_9`에서 regression이 명확해 현재 코드에는 반영하지 않았다.
- **Hypothesis**: `target_entry` pass가 tardy placement를 찾았을 때도 block 단위 release-time 후보를 비교하면, full release fallback의 장점만 흡수하고 forced-heavy 문제는 피할 수 있을 것으로 예상했다.
- **Change locus**: `baseline/athena/placement.py::place_initial`
  - 실험 변경: second pass 조건을 `best is None`에서 `best is None or best[5] > due` 계열로 확장.
  - 좁힌 변형도 확인: `best is None or (best[5] > due and best[4] > release_time)`.
- **Targeted eval**:

  | Run | Pattern | Result |
  |---:|---|---:|
  | 47 | `prob_9.json` | 45,621,213.31. init은 50.16M → 45.62M으로 좋아졌지만 init 시간이 15.3s → 25.9s로 늘어 fallback이 forced 90개로 붕괴 |
  | 48 | `prob_9.json` | 45,621,213.31. 좁힌 조건도 init 26.1s, fallback forced 91개로 동일하게 부적합 |

- **판정 / follow-up**:
  - `place_initial()` 내부에서 tardy block마다 release 후보를 전부 재탐색하면 계산비용이 fallback 예산을 잠식한다.
  - 다음 시도는 block 단위 rescue가 아니라, `smooth_time_windows()`에서 더 싸게 target을 당기거나, release 후보 평가를 top-1/top-2 bay/position으로 제한하는 별도 cheap rescue로 설계해야 한다.

### H-007 — Athena gated release Seed 2 init

- **날짜**: 2026-06-02
- **상태**: **롤백됨**. full `training_set` regression은 통과했지만 직전 full run #39 대비 총합 개선이 `-0.0038%`로 너무 작고, instance별로 5개 악화가 있어 유지하지 않기로 했다. 현재 코드에는 반영되어 있지 않다.
- **Hypothesis**: release-time init은 `n <= 200` 문제에서 자주 Seed 1보다 좋은 후보가 되지만, `n >= 250` dense 문제에서는 forced placement가 급증하고 선택되지 않는 경향이 강하다. 따라서 release Seed 2를 fallback이 아니라 portfolio seed로 유지하되, 큰 instance에서는 skip해서 SA 예산을 돌려주는 편이 낫다.
- **Change locus**: `baseline/athena/entrypoint.py`
  - 기존 `athena.init.fallback` 조건부 pass를 `athena.init.seed2` portfolio pass로 명명 변경.
  - Seed 2 target은 `release_time`.
  - 실행 gate: `not init_res["feasible"] or n <= 200`.
  - Seed 2가 선택된 뒤 repair가 필요하면 event를 `athena.seed2.repair`로 기록.
- **Targeted eval**:

  | Run | Pattern | Result |
  |---:|---|---:|
  | 44 | `prob_9.json` | 43,485,027.85. Seed 1 init 50,156,290.56 → Seed 2 44,141,904.40 selected → SA 43,485,027.85 |
  | 45 | `prob_20.json` | 129,884,685.95. `n=300`이라 Seed 2 skip, H-006 targeted best 수준 유지 |

- **Full regression**:

  | Run | Pattern | Feasible | Aggregate |
  |---:|---|---:|---:|
  | 46 | `*.json`, `--workers 6 --cores-per-worker 4` | 20/20 | total 856,623,527.73 vs run #39 total 856,656,244.23 (**-0.0038%**) |

  Run #46 vs run #39: 개선 2개(`prob_15`, `prob_18`), 악화 5개(`prob_1`, `prob_2`, `prob_17`, `prob_19`, `prob_20`), 동일 13개. 총합은 `prob_18` 개선이 `prob_17/19` 악화를 상쇄해 아주 작게 개선.

- **Caveats / follow-ups**:
  - 이 변경은 대형 improvement라기보다 release Seed 2의 낭비를 줄이는 budget gate다. Full run의 instance별 차이는 parallel SA run-to-run timing 영향이 섞여 있으므로, 단독으로는 강한 APPROVE 근거가 약하다.
  - 실험 중 `release_time + critical order` Seed 2도 확인했으나 `prob_14/20`에서 forced가 늘고 objective가 크게 악화되어 폐기했다.
  - 다음 Seed 2 개선은 단순 release target이 아니라 `target_entry`와 `release_time` 후보를 block placement 단계에서 함께 평가하는 방향이 더 유망하다.

### H-006 — Athena `w1`/slack-aware smoothing 전처리

- **날짜**: 2026-06-02
- **상태**: full `training_set` regression 통과. 직전 full run #35 대비 15개 개선, 5개 동일, regression 0개.
- **Hypothesis**: `smooth_time_windows()`의 기존 `gamma_tard=4.0` 고정 비용은 실제 objective의 `w1` 스케일(대략 8,889~29,630)을 반영하지 못해 tight-slack block을 불필요하게 뒤로 밀었다. Tardiness 후보와 release 이후 delay에 `w1`/slack 기반 penalty를 주면 smoothed init이 release-time fallback의 장점을 일부 흡수하고, 큰 instance에서 fallback deadline 부족으로 forced placement가 폭증하는 문제를 줄일 수 있다.
- **Change locus**: `baseline/athena/features.py::smooth_time_windows`
  - `tard_weight = max(gamma_tard, min(2000.0, 0.05 * w1))`
  - `delay_weight = min(0.10 * tard_weight, 0.001 * w1) * slack_pressure`
  - 후보 시간 cost에 `tard_weight * tard + delay_weight * delay` 추가.
- **Targeted eval**:

  | Run | Pattern | Result |
  |---:|---|---:|
  | 36 | `prob_?0.json` | `prob_10` 17,159,787.78 (**-5.6%** vs best baseline), `prob_20` 129,884,685.95 (**-2.3%** vs previous best) |
  | 37 | `prob_14.json` | 71,605,661.69 (**-9.9%** vs previous best) |
  | 38 | `prob_1.json` | 31,916,611.44 (**-0.4%** vs previous best) |

- **Full regression**:

  | Run | Pattern | Feasible | Aggregate |
  |---:|---|---:|---:|
  | 39 | `*.json`, `--workers 6 --cores-per-worker 4` | 20/20 | total 856,656,244.23 vs run #35 total 987,086,826.01 (**-13.2%**) |

  Run #39 vs 직전 full run #35: 15개 개선, 5개 동일, regression 0개. 큰 개선은 `prob_20` 182.39M → 132.37M, `prob_13` 106.04M → 81.49M, `prob_14` 84.26M → 71.62M, `prob_18` 88.13M → 76.94M.

- **Caveats / follow-ups**:
  - `eval_summary --baseline-window 5`는 targeted probe run #36~#38까지 baseline pool에 포함하므로 `prob_1/14/20`이 targeted best 대비 소폭 worse로 보인다. 직전 full run #35와의 공정 비교에서는 regression 0개.
  - Release-time fallback은 큰 dense instance에서 여전히 forced가 많이 튄다(`prob_20` fallback forced 179). 이번 변경은 smoothed init을 강화해 그 fallback을 선택하지 않게 만든 것이고, fallback 병렬화/예산 개선은 별도 후속 과제다.
  - `progress.md`는 앞으로 Codex 세션의 개선/실험도 이 섹션에 계속 기록한다.

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
