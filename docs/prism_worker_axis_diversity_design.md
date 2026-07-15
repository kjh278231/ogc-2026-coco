# PRISM 포트폴리오 워커 축 다양성(axis diversity) 설계

**상태**: 설계 완료, 구현 전. 사용자 요청(2026-07-15): "4코어 예산이 앵커 다양성 한 축에 전부 소진됨"에 대한 해결 설계.

---

## 0. 문제 정의

PRISM의 포트폴리오(`prism/portfolio.py`)는 4개 워커 프로세스를 스폰하고, 각 워커는 서로 다른 **앵커**(`pref`/`balanced`/`capped`/`mip16`, `prism_engine.py:158-182`의 `_anchors()`가 1회 계산)에서 출발해 `refine_anchor` → `_refine`(`prism_engine.py:185-242`)로 정제한다. 워커 간 차이는 **(a) 어느 앵커에서 출발하는가**와 **(b) mover-shuffle용 RNG 시드**(`20260629 + 1000*i`, `portfolio.py:113`) 뿐이다.

그런데 같은 공유 커널(`bridge/solver.py`/`bridge/packing.py`)에는 **앵커와 무관하게 독립적으로 검증된 다양성 축**이 이미 3개 더 존재하고, 각각 실측 근거가 있다:

| 축 | 검증 근거 | PRISM에서의 현재 상태 |
|---|---|---|
| **L** (LAHC 이력 길이) | `bridge/portfolio.py:54` PROFILES 중 L=30 워커가 "uphill subset: T6/T17/T18" 전담 승리 | 4워커 전부 `L=1` 고정 (`portfolio.py:78`에서 1회 계산 후 파라미터로 관통) |
| **R** (탐색 시 마스크 해상도) | `docs/experiment_board.md:84` R-sweep: best-of(R4,R8,R16) vs 단일R8 = **−11.1%**, "최적 R이 인스턴스별 전부 다름"(R16=fine-geometry T3/T6 승, R4=eval-starved T11/T20 승) | 4워커 전부 `SOLVER_MASK_SEARCH_R=8` 고정 |
| **fresh-restart 다양성** (`SOLVER_LAHC_DIVERSE`류: 매 스텝 `best`를 킥하는 대신, 가끔 원본 앵커에서 새 shuffle로 재하강) | `bridge/portfolio.py:41-44`: 이 메커니즘의 워커가 "T1 −49%, T20 −27%" 트랩 탈출 | **`prism/`, `flux/`, `helm/`, `weave/` 전체에 이 메커니즘 자체가 없음**(grep 확인). `_refine`의 ILS 루프(`prism_engine.py:232-241`)는 항상 `K._perturb(prob, best, ...)`로만 킥 — 원본 앵커로의 재하강 경로가 없음 |

**직접 증거**: `docs/prism_experiment_log.md`는 PRISM의 T18 손실을 "genuine search-coverage gap — no PRISM anchor/recombine reaches T18's good basin; BRIDGE's div01/L-diverse workers do"라고 명시한다. 이건 이 표의 세 번째 축이 없어서 생긴 실측 손실이며, 나중에 HELM이 **PRISM 자체를 고치지 않고 다른 엔진으로 라우팅**해서 우회했다(T18 50074, "프로젝트 역대 최고"). 즉 지금 이 갭은 PRISM 내부 개선이 아니라 외부 우회로만 닫혀 있다.

## 1. 가설 (반증 가능한 형태)

> 앵커 선택은 그대로 두고, 4개 워커 중 baseline 1개를 제외한 나머지에 L/R/fresh-restart 축을 하나씩 추가로 얹으면, 포트폴리오의 best-of 후보 풀이 넓어져 순수 회귀 없이 (Pareto-safe) 추가 승리를 얻는다 — 특히 T18(coverage gap), T6/T17(L=30 승리 이력), T3/T6(R16 승리 이력) 구간에서.

**반증 조건**: 아래 §5의 Tier 1/Tier 2 A/B에서 baseline 대비 개선이 전혀 없거나 순손실만 나오면 기각.

## 2. 설계

### 2.1 원칙 — worker 0은 절대 건드리지 않는다 (Pareto-safety의 핵심)

포트폴리오 마스터는 이미 모든 워커의 결과를 `K._score_and_pack`로 진짜 채점해 best-of를 뽑고(`portfolio.py:173-230`), 전체 워커 pool을 합쳐 union-recombine 1회를 돌린다. **worker 0을 오늘의 정확한 동작(anchor=pref, L=1, R=8, restart-diversity 없음)으로 그대로 두면, 나머지 워커에 축을 추가하는 것은 best-of 후보를 늘리는 것일 뿐 — 포트폴리오 전체 출력이 오늘보다 나빠질 길이 없다.** 이게 이 설계 전체의 안전성 근거다.

### 2.2 `prism/portfolio.py` — `_WORKER_AXES` 테이블

`_WORKER_FIXED`(28번 줄) 바로 뒤, `def _worker`(35번 줄) 앞에 추가:

```python
# Per-worker axis diversity, ADDITIONAL to anchor choice (env-gated by
# PRISM_PORTF_DIVERSE_AXES, default OFF -> today's behaviour is bit-identical). Keyed
# by WORKER POSITION i (0..3), NOT anchor name -- degrades gracefully when _HAS_GUROBI
# is False (3 heuristic anchors, row 3 unused) or PRISM_HEUR_ANCHORS/PRISM_LAMBDAS
# reshapes the spectrum (whatever anchor lands at position i gets row i's axis).
#   w0 pref     -- BASELINE, unchanged (Pareto-safety anchor)
#   w1 balanced -- LAHC L=30 (bridge/portfolio.py:54: "uphill subset T6/T17/T18")
#   w2 capped   -- fresh-restart diversity (closes the T18 coverage-gap mechanism)
#   w3 mip16    -- search-time mask R=16 (R-sweep: wins fine-geometry T3/T6)
_WORKER_AXES = [
    {},
    {"L": 30},
    {"PRISM_REFINE_DIVERSE": "1"},
    {"SOLVER_MASK_SEARCH_R": "16"},
]


def _axis_for(i):
    return _WORKER_AXES[i] if 0 <= i < len(_WORKER_AXES) else {}


def _axis_L(i, default_L):
    return _axis_for(i).get("L", default_L)


def _axis_env(i):
    return {k: v for k, v in _axis_for(i).items() if k != "L"}
```

`L`은 특별 취급한다 — PRISM이 이미 `L`을 환경변수가 아니라 **함수 파라미터로 관통**시키기 때문이다(`portfolio.py:78`에서 1회 읽고 `payloads`에 실어 전달; `_climb_lahc`까지 어디서도 재읽기 없음, 확인 완료). 나머지 키는 워커 프로세스 안에서 `os.environ`에 그대로 적용한다.

### 2.3 `_worker` 시그니처 변경 (현재 35-49번 줄)

```python
def _worker(prob, anchor, worker_tl, L, seed, env_over=None):
    """... `env_over` (PRISM_PORTF_DIVERSE_AXES가 켜졌을 때만 non-None): 이 워커의 추가
    축 오버라이드(SOLVER_MASK_SEARCH_R, PRISM_REFINE_DIVERSE). `import prism_engine` 전에
    적용해야 한다 -- bridge/packing.py의 _MASK_R_SEARCH는 이 프로세스에서 packing이 처음
    import될 때 얼어붙는 모듈 전역이라, import 이후 환경변수를 바꿔도 무효다. 모든 워커는
    매번 새로 spawn되는 프로세스(fork 아님)이므로 여기서는 항상 그 import 이전이다."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    for k, v in _WORKER_FIXED.items():
        os.environ[k] = v
    if env_over:
        for k, v in env_over.items():
            os.environ[k] = v
    import prism_engine as P
    t = time.time()
    try:
        best, pool, tot = P.refine_anchor(prob, anchor, worker_tl, L=L, seed=seed)
        return {"best": best, "pool": pool, "tot": tot, "elapsed": time.time() - t, "err": None}
    except Exception as e:                                  # pragma: no cover
        return {"best": None, "pool": {}, "tot": None, "elapsed": time.time() - t, "err": repr(e)}
```

`env_over=None`이 기본값이므로, 게이트가 꺼져 있으면 `if env_over:` 블록이 실행되지 않아 기존 5-인자 호출과 완전히 동일하게 동작한다.

### 2.4 payload 구성 (현재 108-114번 줄)

```python
    n = min(len(anchors), int(os.environ.get("PRISM_PORTFOLIO_WORKERS", str(len(anchors)))))
    diverse_axes = P._env_flag("PRISM_PORTF_DIVERSE_AXES", False)
    LAST["diverse_axes"] = diverse_axes
    results = []
    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        if diverse_axes:
            payloads = [(prob, anchors[i][1], worker_tl, _axis_L(i, L),
                         20260629 + 1000 * i, _axis_env(i)) for i in range(n)]
            LAST["worker_axes"] = [_axis_for(i) for i in range(n)]
        else:
            payloads = [(prob, anchors[i][1], worker_tl, L, 20260629 + 1000 * i)
                        for i in range(n)]
        with ctx.Pool(n) as pool:
            asyncs = [pool.apply_async(_worker, p) for p in payloads]
            ...  # 117번 줄부터 원본 그대로
```

`else` 분기는 원본 113번 줄과 글자 그대로 동일하다 — 게이트 off일 때 이 코드는 손대지 않은 것과 동치다. `P._env_flag(name, False)`는 이 파일이 이미 default-off 플래그(`PRISM_NO_MIP`)에 쓰는 관용구와 동일한 패턴이다. `LAST["worker_axes"]`는 `tools/_prism_portf_ab.py`가 이미 `portfolio.LAST` 전체를 stderr에 덤프하므로(43번 줄) 코드 변경 없이 진단에 그대로 노출된다.

**idle-reclaim(197-216번 줄)은 의도적으로 그대로 둔다** — 이건 워커 풀이 아니라 마스터 프로세스에서 도는 별개 단계이고, 마스터는 `_worker`의 env-before-import 트릭을 거치지 않으므로 이 자리에서 R을 바꾸는 것은 범위 밖이다.

### 2.5 `prism/prism_engine.py` — `_refine`에 fresh-restart 이식

`_refine`/`refine_anchor`에 `seed`를 관통시킨다(현재는 `rng`만 받음):

```python
def _refine(prob, anchor, cache, dl, L, rng=None, seed=None):
```

```python
# refine_anchor (245-262번 줄) 안, rng 생성 직후:
    rng = random.Random(seed) if seed is not None else None
    A, tot = _refine(prob, anchor, cache, dl, L, rng=rng, seed=seed)
```

`prism_solve`의 기존 호출부(336번 줄)는 `seed`를 넘기지 않으므로 `seed=None`으로 떨어져 시리얼 경로는 무변경이다. (`rng.randrange()`로 파생 시드를 뽑는 대신 `seed`를 직접 관통시키는 이유: outer `rng` 스트림을 추가로 소모하면 게이트 off/on 간 킥 궤적이 미묘하게 달라져 "게이트 off=bit-identical" 보장이 깨진다.)

`_refine` 본문, `_eject` 정의(226번 줄) 직후 삽입:

```python
    # Fresh-restart diversity (PRISM_REFINE_DIVERSE, default OFF -> 아래 루프는 오늘과
    # bit-identical). bridge/solver.py의 SOLVER_LAHC_DIVERSE(~1134-1166번 줄)를 _refine의
    # 더 단순한 descent->swap->eject->ILS 루프 구조에 맞게 이식. 매 ILS 스텝이 running
    # best를 킥(2-5 블록 재배치, best의 이웃만 탐색)하는 대신, PRISM_REFINE_DIVERSE_EVERY
    # 번째마다(기본 2 = 번갈아) 원본 앵커 A0에서 독립 시드로 새로 shuffle해 통째로
    # 재하강한다 -- best의 이웃이 아니라 완전히 다른 Z1=0 분지에 도달한다. best는 두
    # 분기 모두 동일한 방식으로 running-min을 유지하므로 Pareto-safe(추가만 하지 대체 X).
    _diverse = _env_flag("PRISM_REFINE_DIVERSE")
    _diverse_every = max(1, int(os.environ.get("PRISM_REFINE_DIVERSE_EVERY", "2")))
    _dseed = seed if seed is not None else int(os.environ.get("SOLVER_SEED", "20260629"))
    best, best_tot = K._climb_lahc(prob, A0, cache, dl, L, rng=rng)
```

ILS 루프(232-241번 줄) 변경:

```python
    _it = 0
    while K._within(dl):
        if _diverse and (_it % _diverse_every) == (_diverse_every - 1):
            wrng = random.Random(_dseed * 1000003 + _it)
            cand = dict(A0)
            cur, tot = K._climb_lahc(prob, cand, cache, dl, L, rng=wrng)
        else:
            cand = K._perturb(prob, best, cache, rng)
            cur, tot = K._climb_lahc(prob, cand, cache, dl, L, rng=rng)
        if _swap:
            cur, tot = K._z3_refine(prob, cur, cache, dl)
        if _eject:
            cur, tot = K._ejection_refine(prob, cur, cache, dl)
        if tot < best_tot - 1e-9:
            best, best_tot = cur, tot
        _it += 1
    return best, best_tot
```

`_diverse=False`(게이트 off)면 매 반복이 항상 `else` 분기로 떨어지고, 그 분기는 원본 234-235번 줄과 글자 그대로 동일 — bit-identical. `PRISM_REFINE_DIVERSE_EVERY=1`은 매 스텝이 fresh-restart인 극단값(bridge 원형과 동일), 기본값 2는 "번갈아"다.

## 3. 엣지 케이스

- **`_HAS_GUROBI=False`** (mip16 없이 3-anchor): `_axis_for(3)`은 테이블 밖이라 `{}` — R=16 축이 이번 실행에 그냥 미적용(크래시 없음). index 기반 결합은 정확히 이 케이스를 위한 설계다.
- **`PRISM_HEUR_ANCHORS`/`PRISM_LAMBDAS` 커스터마이징**: 앵커 이름이 아니라 **위치**로 축이 배정되므로, 앵커 스펙트럼을 바꾸면 축 배정도 따라간다(예: `PRISM_LAMBDAS="1,8,16"`이면 w1=mip1이 L=30을 받음). 부작용이 아니라 설계의 직접적 귀결 — 문서화만 필요.
- **워커 수 n**: override 없으면 `n == len(anchors)`이고, 정상 범위는 Gurobi 유무에 따라 3 또는 4다. `n > 4`(예: `PRISM_LAMBDAS`를 여러 값으로 확장)면 `_axis_for(i>=4)`가 `{}`를 반환해 5번째 이후 워커는 축 없는 추가 앵커 워커로 안전하게 동작한다.

## 4. 검증 계획

### Tier 1 — 고정-eval 축 격리 프로브 (신규, 결정적, 멀티프로세싱 없음)

`tools/_prism_ab.py`와 같은 스타일로 신규 `tools/_prism_axis_ab.py`를 만들어, `refine_anchor`를 **직접, 앵커별로** 호출한다(포트폴리오/멀티프로세싱 우회). `refine_anchor`가 호출마다 `K._POOL`/`K._EVALS`/`K._EVAL_LIMIT`을 리셋하므로(251-254번 줄) 반복 호출이 안전하지만, **`K._MASK_R_SEARCH`는 리셋되지 않는 모듈 전역**이라 매 axis 루프 시작 시 명시적으로 8로 되돌려야 한다. (트릭: `bridge/solver.py`가 `from packing import (..., _MASK_R_SEARCH, ...)`로 이름을 자기 네임스페이스에 복사해오므로, import 이후라도 `K._MASK_R_SEARCH = 16`으로 재대입하면 다음 호출부터 즉시 반영된다 — 서브프로세스 재시작 없이 한 프로세스 안에서 R을 스윕할 수 있다.)

```python
"""tools/_prism_axis_ab.py -- Tier 1: L/R/fresh-restart 축을 refine_anchor 직접 호출로
격리 검증. 고정 eval, 멀티프로세싱 없음, tools/_prism_ab.py와 동일한 출력 스타일.

Usage: python tools/_prism_axis_ab.py <axis> <inst> [anchor=capped] [evals=4000]
       axis: baseline | L30 | diverse | diverse1 | diverse3 | R16 | R4 | guided
"""
import os, sys, json, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "prism"))

axis = sys.argv[1]
inst = sys.argv[2]
anchor_name_want = sys.argv[3] if len(sys.argv) > 3 else "capped"
evals = int(sys.argv[4]) if len(sys.argv) > 4 else 4000

for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_MASK_PREPARE", "1"), ("SOLVER_SWAP", "1"), ("SOLVER_CP_WORKERS", "1")):
    os.environ.setdefault(k, v)

import prism_engine as P
K = P.K

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
anchors = dict(P._anchors(prob, mip_tl=4.0, want_mip=True))
anchor_name = anchor_name_want if anchor_name_want in anchors else next(iter(anchors))
anchor = anchors[anchor_name]

# 매 axis 실행 전 baseline으로 명시적 리셋 (refine_anchor가 안 건드리는 상태)
K._MASK_R_SEARCH = 8
os.environ.pop("PRISM_REFINE_DIVERSE", None)
os.environ.pop("PRISM_REFINE_DIVERSE_EVERY", None)
os.environ.pop("SOLVER_GUIDED", None)

L, seed = 1, 20260629
if axis == "L30":
    L = 30
elif axis == "diverse":
    os.environ["PRISM_REFINE_DIVERSE"] = "1"
elif axis == "diverse1":
    os.environ.update(PRISM_REFINE_DIVERSE="1", PRISM_REFINE_DIVERSE_EVERY="1")
elif axis == "diverse3":
    os.environ.update(PRISM_REFINE_DIVERSE="1", PRISM_REFINE_DIVERSE_EVERY="3")
elif axis == "R16":
    K._MASK_R_SEARCH = 16
elif axis == "R4":
    K._MASK_R_SEARCH = 4
elif axis == "guided":
    os.environ["SOLVER_GUIDED"] = "1"
elif axis != "baseline":
    print("unknown axis", axis); sys.exit(1)

t0 = time.time()
best, pool, tot = P.refine_anchor(prob, anchor, timelimit=None, L=L, eval_limit=evals, seed=seed)
wall = time.time() - t0
obj, packed = K._score_and_pack(prob, best, poly_deadline=None)
z2, z3 = K.obj23(prob, best)
w = prob["weights"]
z1 = round((obj - w["w2"] * z2 - w["w3"] * z3) / w["w1"])
print(json.dumps({"axis": axis, "inst": inst, "anchor": anchor_name, "evals": evals,
                  "obj": round(obj), "z1z2z3": [z1, round(z2), round(z3)],
                  "pool": len(pool), "wall_s": round(wall, 1)}))
```

**축별 타깃 인스턴스** (근거 명시):

| 축 | 앵커 | 인스턴스 | 근거 |
|---|---|---|---|
| L=30 | balanced | **T6, T17, T18** | `bridge/portfolio.py:54` "uphill subset: T6/T17/T18" |
| fresh-restart | capped | **T1, T18, T20** (`PRISM_REFINE_DIVERSE_EVERY` ∈ {1,2,3} 스윕) | `bridge/portfolio.py:41-44` div01 실측 + PRISM T18 coverage-gap 진단(`docs/prism_experiment_log.md`) |
| R=16 | mip16 | **T3, T6**(R16 우세 기대) vs **T11, T18, T20**(R4/eval-starved 우세 기대) | `docs/experiment_board.md:84` R-sweep |

**주의**: R축은 고정-eval에서 **R16이 R8/R4보다 eval당 wall-cost가 더 높다**(마스크 precompute가 R²에 비례). Tier 1은 "같은 탐색 노력에서 어느 R이 더 나은 분지에 도달하는가"만 답하고 "같은 wall-time에서 어느 R이 이기는가"는 답하지 못한다 — **R축의 최종 채택 판단은 반드시 Tier 2(wall-clock)로** 한다. L/fresh-restart 축은 eval당 비용이 거의 그대로라 Tier 1을 더 직접적으로 신뢰할 수 있다.

### Tier 2 — 실배포 wall-clock A/B (기존 도구 재사용, 신규 스크립트 불필요)

`tools/_prism_portf_ab.py <prism|bridge> <inst> <timelimit>`가 이미 실제 `algorithm()` 엔트리포인트를 통한 wall-clock A/B 도구이고, 새 게이트가 env-gated이므로 코드 변경 없이 toggle만으로 재사용 가능:

```powershell
python tools/_prism_portf_ab.py prism T18 180        # control (게이트 off)
$env:PRISM_PORTF_DIVERSE_AXES = "1"
python tools/_prism_portf_ab.py prism T18 180        # candidate (게이트 on)
Remove-Item Env:\PRISM_PORTF_DIVERSE_AXES
```

`portfolio.LAST`가 stderr에 덤프되므로(`_prism_portf_ab.py:43`) `LAST["worker_axes"]`가 공짜로 노출되어 어느 워커가 실제로 어떤 축을 받았는지 즉시 확인된다.

전체 20-instance 회귀 게이트는 `tools/run_eval.py --compare`(같은 timelimit에서만 허용, `run_eval.py:112-115`):

```powershell
python tools/run_eval.py --solver prism --instances "train/*.json" --timelimit 60 `
    --out results_prism_axesoff_t60.json
$env:PRISM_PORTF_DIVERSE_AXES = "1"
python tools/run_eval.py --solver prism --instances "train/*.json" --timelimit 60 `
    --out results_prism_axeson_t60.json --compare results_prism_axesoff_t60.json
Remove-Item Env:\PRISM_PORTF_DIVERSE_AXES
```

T=180, T=300에도 동일 반복. `--compare`는 wins/losses/ties/aggregate delta를 자동 출력하고(`run_eval.py:146-161`), overrun 체크(`wall > timelimit*1.05`)도 자동 포함된다(`run_eval.py:138-139`) — 별도 안전성 스크립트가 필요 없다.

**게이트 off = bit-identical의 실증 확인**: `PRISM_PORTF_DIVERSE_AXES`를 설정하지 않은 채 만든 `results_prism_axesoff_t*.json`의 objective가 기존 로그(`tools/_prism_portf_ab180.txt` 등, 예 T13 159779·T20 505861·T17 66273)와 정확히 일치하는지 대조 — 코드 변경이 게이트-off 경로를 전혀 건드리지 않았다는 가장 강한 증거.

## 5. 롤아웃 (이 저장소의 기존 관례 그대로)

이 저장소의 모든 미검증 레버(`SOLVER_SWAP`, `SOLVER_MULTIORDER`, `PRISM_REPAIR_FIRST` 등)는 자기 모듈에서 default off로 시작해 검증 후 `prism/myalgorithm.py`의 `os.environ.setdefault(...)` 목록(현재 9-29번 줄)에서 기본 on으로 승격된다. 동일 절차:

1. **이 설계 구현 시점**: `PRISM_PORTF_DIVERSE_AXES`/`PRISM_REFINE_DIVERSE`는 각자 모듈에서 `_env_flag(..., False)`로만 읽힘 — default off. **`prism/myalgorithm.py`는 건드리지 않는다.**
2. **Tier 1 + Tier 2 모두 통과 후**: `myalgorithm.py`에 `os.environ.setdefault("PRISM_PORTF_DIVERSE_AXES", "1")` 한 줄 추가 — `SOLVER_SWAP` 승격과 동일 절차.

## 6. 리스크 / 열린 질문

1. **R축의 import-freeze 위험**: 어떤 그레이더 하네스가 `packing`/`solver`를 최상위에서 미리 import해버리면 R축은 조용히 무력화(크래시 아님, R=8로 동작)된다. `_WORKER_FIXED`도 이미 같은 가정에 의존하므로 새로운 위험 등급은 아니지만, `PRISM_PORTF_DEBUG=1` 덤프에 `_MASK_R_SEARCH` 실효값 로깅을 추가하면 조기 발견 가능.
2. **Tier 1 스크립트의 상태 누수**: `K._MASK_R_SEARCH`/`PRISM_REFINE_DIVERSE*`/`SOLVER_GUIDED`는 `refine_anchor`가 리셋하지 않으므로 여러 axis를 한 프로세스에서 순회할 때 매 iteration 시작에 명시적 리셋 필수(§4 스크립트에 반영됨).
3. **worker1(L=30)/worker2(diverse) wall 오버런 여부**: `worker_tl` 계산은 축과 무관하게 동일(`portfolio.py:106`)하고 각 워커는 자기 `dl`을 스스로 체크하므로 새 오버런 경로는 이론상 없지만, Tier 2에서 wall 컬럼으로 재확인 권장.
4. **guided-destroy는 5번째 축으로 배제**: 우선순위상 낮음(노이즈 큼, "blanket 기본값은 net −1.4%"). `K._perturb`가 이미 `SOLVER_GUIDED`를 라이브로 읽으므로 코드 변경 없이 워커 dict에 한 줄 추가하는 것만으로 확장 가능 — 필요 시 향후 슬롯으로만 문서화.

## 7. 범위 밖 (명시적으로 배제)

- **워커 수를 4 초과로 확장**하는 것 — 그레이더의 실제 코어 수가 확인되지 않았고, `PRISM_PORTFOLIO_MIN_T`/4-worker 상한 자체가 "코어 2개뿐인 환경에서도 안전"이라는 경험적 근거로 하드코딩되어 있다. 이 설계는 **주어진 4슬롯을 더 잘 쓰는 것**이지 슬롯을 늘리는 것이 아니다.
- **`SOLVER_EJECTION`의 예산 고갈 문제 해결** — 별도 이슈(ejection은 코드가 있지만 LAHC+swap이 예산을 다 쓴 뒤라 실행 기회를 못 받는다는 것이 이미 실측 확인됨). 이 설계와 독립적으로 다뤄야 한다.
- **HELM류 인스턴스-적응 라우팅으로 확장** — 이 설계(정적 축 테이블)가 먼저 검증된 뒤, 인스턴스 특징 기반으로 축 배정 자체를 동적으로 고르는 것이 자연스러운 다음 단계다.
