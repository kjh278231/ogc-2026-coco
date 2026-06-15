"""Independent covering-pool solver prototype for OGC 2026.

This is intentionally separate from solver.framework_solve.  It reuses only the
validated per-bay packing/building kernel from solver.py, while the assignment
engine is different:

    seed assignments -> bay-set column pool -> set covering + dedup MIP
    -> ILS-like perturbation to create new columns -> repeat.

The MIP is used as a candidate generator, not as a proof of final quality.  Every
candidate is converted back to a true assignment and rescored by the same per-bay
packer before it can become the incumbent.
"""
from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass

import solver as _kernel

try:
    from ortools.sat.python import cp_model as _cp_model
    _HAS_ORTOOLS = True
except Exception:  # pragma: no cover
    _HAS_ORTOOLS = False

LAST_STATS: dict = {}


@dataclass(frozen=True)
class Column:
    bay: int
    ids: tuple[int, ...]
    tardiness: float
    workload: float
    pref_loss: float


def _clear_kernel_caches():
    """solver.py caches are id(block_data)-keyed; clear them per problem."""
    for name in ("_LOCAL_FP", "_LOCAL_BOX", "_ORIENT_BBOX"):
        cache = getattr(_kernel, name, None)
        if isinstance(cache, dict):
            cache.clear()


def _pref_loss(block, bay):
    prefs = block["bay_preferences"]
    return max(prefs) - prefs[bay]


def _fit_options(prob):
    blocks = prob["blocks"]
    bays = prob["bays"]
    opts = []
    for b in blocks:
        js = [j for j, bay in enumerate(bays) if _kernel.fits(b, bay)]
        opts.append(js or list(range(len(bays))))
    return opts


class ColumnPool:
    def __init__(self, prob):
        self.prob = prob
        self.blocks = prob["blocks"]
        self.bays = prob["bays"]
        self.columns: dict[tuple[int, tuple[int, ...]], Column] = {}

    def column(self, bay: int, ids) -> Column | None:
        ids = tuple(sorted(ids))
        if not ids:
            return None
        key = (bay, ids)
        col = self.columns.get(key)
        if col is not None:
            return col
        placed = _kernel.solve_bay(self.prob, bay, list(ids), poly=False)
        tard, _ = _kernel.extract_tardiness(self.prob, bay, placed)
        workload = sum(self.blocks[i]["workload"] for i in ids)
        pref = sum(_pref_loss(self.blocks[i], bay) for i in ids)
        col = Column(bay, ids, tard, workload, pref)
        self.columns[key] = col
        return col

    def add_assignment(self, assign: dict[int, int]):
        m = len(self.bays)
        out = []
        for j in range(m):
            ids = [i for i, a in assign.items() if a == j]
            col = self.column(j, ids)
            if col is not None:
                out.append(col)
        return out

    def proxy_score(self, assign: dict[int, int]) -> float:
        cols = self.add_assignment(assign)
        obj1 = sum(c.tardiness for c in cols)
        obj2, obj3 = _kernel.obj23(self.prob, assign)
        w = self.prob["weights"]
        return w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3


def _seed_pref(prob, opts):
    blocks = prob["blocks"]
    return {
        i: max(opts[i], key=lambda j: blocks[i]["bay_preferences"][j])
        for i in range(len(blocks))
    }


def _seed_balanced(prob, opts):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    u = [sum(areas) / m / a for a in areas]
    loads = [0.0] * m
    assign = {}
    for i in sorted(range(len(blocks)), key=lambda k: -blocks[k]["workload"]):
        j = min(opts[i], key=lambda b: u[b] * (loads[b] + blocks[i]["workload"]))
        assign[i] = j
        loads[j] += blocks[i]["workload"]
    return assign


def _seed_pref_capped(prob, opts, cap_factor=1.15):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    cap = max(1, math.ceil(len(blocks) / m * cap_factor))
    counts = [0] * m
    assign = {}
    order = sorted(
        range(len(blocks)),
        key=lambda i: (
            blocks[i]["due_date"],
            -max(blocks[i]["bay_preferences"][j] for j in opts[i]),
            -blocks[i]["workload"],
        ),
    )
    for i in order:
        prefs = blocks[i]["bay_preferences"]
        choices = sorted(opts[i], key=lambda j: -prefs[j])
        j = next((bay for bay in choices if counts[bay] < cap), choices[0])
        assign[i] = j
        counts[j] += 1
    return assign


def _seed_z23_weighted(prob, opts):
    """Greedy seed that directly scalarizes assignment-only Z2/Z3.

    The earlier independent seeds either followed pure preference or mostly
    balanced workload.  This one uses the documented objective weights on the
    two assignment-only terms, so it is still cheap but less one-eyed.
    """
    blocks = prob["blocks"]
    bays = prob["bays"]
    w = prob["weights"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    loads = [0.0] * m
    assign = {}

    def pref_regret(i):
        prefs = sorted((blocks[i]["bay_preferences"][j] for j in opts[i]), reverse=True)
        return prefs[0] - (prefs[1] if len(prefs) > 1 else prefs[0])

    def z2_after(j, workload):
        vals = [u[b] * (loads[b] + (workload if b == j else 0.0)) for b in range(m)]
        return max(
            (abs(vals[a] - vals[b]) for a in range(m) for b in range(m) if a != b),
            default=0.0,
        )

    order = sorted(
        range(len(blocks)),
        key=lambda i: (
            -pref_regret(i),
            blocks[i]["due_date"],
            blocks[i]["release_time"],
            -blocks[i]["workload"],
        ),
    )
    for i in order:
        b = blocks[i]
        prefs = b["bay_preferences"]
        max_pref = max(prefs[j] for j in opts[i])
        j = min(
            opts[i],
            key=lambda bay: (
                w["w2"] * z2_after(bay, b["workload"])
                + w["w3"] * (max_pref - prefs[bay]),
                max_pref - prefs[bay],
                u[bay] * (loads[bay] + b["workload"]),
            ),
        )
        assign[i] = j
        loads[j] += b["workload"]
    return assign


def _seed_due_balanced(prob, opts):
    """Preference-aware load seed in EDD order, distinct from BRIDGE's seeds."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    u = [sum(areas) / m / a for a in areas]
    loads = [0.0] * m
    assign = {}
    order = sorted(
        range(len(blocks)),
        key=lambda i: (blocks[i]["due_date"], blocks[i]["release_time"], -blocks[i]["workload"]),
    )
    for i in order:
        b = blocks[i]
        max_pref = max(b["bay_preferences"])
        j = min(
            opts[i],
            key=lambda bay: (
                u[bay] * (loads[bay] + b["workload"]),
                max_pref - b["bay_preferences"][bay],
            ),
        )
        assign[i] = j
        loads[j] += b["workload"]
    return assign


def _loads(prob, assign):
    m = len(prob["bays"])
    loads = [0.0] * m
    for i, j in assign.items():
        loads[j] += prob["blocks"][i]["workload"]
    return loads


def _mutate(prob, assign, opts, rng: random.Random, strength: int):
    """ILS-like random re-homing, biased toward underloaded feasible bays."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    u = [sum(areas) / m / a for a in areas]
    cur = dict(assign)
    ids = list(cur)
    loads = _loads(prob, cur)
    for _ in range(strength):
        i = rng.choice(ids)
        choices = [j for j in opts[i] if j != cur[i]]
        if not choices:
            continue
        # Mostly explore underloaded bays, sometimes pure random for diversity.
        if rng.random() < 0.75:
            j = min(choices, key=lambda b: u[b] * (loads[b] + blocks[i]["workload"]))
        else:
            j = rng.choice(choices)
        loads[cur[i]] -= blocks[i]["workload"]
        cur[i] = j
        loads[j] += blocks[i]["workload"]
    return cur


def _mutate_preference(prob, assign, opts, rng: random.Random, strength: int):
    """Preference-preserving pool branch for low-tardy instances.

    The main mutate branch mostly explores underloaded bays.  This branch instead
    creates columns near high-preference assignments, so covering has material in
    preference-dominant basins rather than only load-balanced ones.
    """
    blocks = prob["blocks"]
    cur = dict(assign)
    improvers = [
        i for i, j in cur.items()
        if any(blocks[i]["bay_preferences"][b] > blocks[i]["bay_preferences"][j] for b in opts[i])
    ]
    if not improvers:
        return cur
    for _ in range(strength):
        i = rng.choice(improvers)
        prefs = blocks[i]["bay_preferences"]
        cur_pref = prefs[cur[i]]
        better = [j for j in opts[i] if j != cur[i] and prefs[j] > cur_pref]
        if not better:
            continue
        if rng.random() < 0.85:
            j = max(better, key=lambda b: prefs[b])
        else:
            top = sorted(better, key=lambda b: -prefs[b])[: min(3, len(better))]
            j = rng.choice(top)
        cur[i] = j
    return cur


def _assignment_z23_score(prob, assign):
    obj2, obj3 = _kernel.obj23(prob, assign)
    w = prob["weights"]
    return w["w2"] * obj2 + w["w3"] * obj3


def _mutate_z23(prob, assign, opts, rng: random.Random, strength: int):
    """Assignment-only local perturbation for the Z2/Z3 landscape.

    z23 seed improved the initial pool, but the normal perturb branch still mostly
    chases underloaded bays. This branch keeps the move cheap: sample blocks, try
    feasible bay moves, and accept moves that improve the documented assignment
    terms before expensive per-bay packing is consulted by the pool scorer.
    """
    blocks = prob["blocks"]
    cur = dict(assign)
    ids = list(cur)
    if not ids:
        return cur
    cur_score = _assignment_z23_score(prob, cur)
    for _ in range(strength):
        sample = rng.sample(ids, min(len(ids), 24))
        sample.sort(
            key=lambda i: _pref_loss(blocks[i], cur[i]) * prob["weights"]["w3"],
            reverse=True,
        )
        best = None
        best_score = cur_score
        for i in sample[:8]:
            choices = [j for j in opts[i] if j != cur[i]]
            if not choices:
                continue
            scored = []
            for j in choices:
                trial = dict(cur)
                trial[i] = j
                score = _assignment_z23_score(prob, trial)
                scored.append((score, i, j))
            if not scored:
                continue
            scored.sort(key=lambda item: item[0])
            score, bi, bj = scored[0] if rng.random() < 0.85 else rng.choice(scored[: min(3, len(scored))])
            if score < best_score - 1e-9:
                best = (bi, bj)
                best_score = score
        if best is None:
            break
        bi, bj = best
        cur[bi] = bj
        cur_score = best_score
    return cur


def _assign_key(assign: dict[int, int]):
    return tuple(assign[i] for i in range(len(assign)))


def _true_obj(prob, assign, deadline):
    """Best-of(AABB, polygon) objective used as the prototype's true assignment score."""
    return _kernel._bestof_obj(prob, assign, deadline=deadline)


def _score_and_pack(prob, assign, deadline, with_components=False):
    """Score an assignment and keep the exact packed bays used for that score.

    The final selection step is expensive because polygon packing is the same work
    needed to emit the solution.  Keeping the selected packing avoids spending the
    polygon budget once for scoring and again for output.
    """
    w = prob["weights"]
    m = len(prob["bays"])
    obj1 = 0.0
    packed = []
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        placed = _kernel.solve_bay(prob, j, ids, poly=False)
        best_t, exits = _kernel.extract_tardiness(prob, j, placed)
        best_placed = placed
        best_exits = exits
        use_poly = (
            not os.environ.get("SOLVER_NOPOLY")
            and (deadline is None or time.time() < deadline)
        )
        if use_poly:
            placed_p = _kernel.solve_bay(prob, j, ids, poly=True, deadline=deadline)
            t_poly, exits_p = _kernel.extract_tardiness(prob, j, placed_p)
            if t_poly < best_t:
                best_t = t_poly
                best_placed = placed_p
                best_exits = exits_p
        packed.append((j, best_placed, best_exits))
        obj1 += best_t
    obj2, obj3 = _kernel.obj23(prob, assign)
    total = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    if with_components:
        return total, packed, (obj1, obj2, obj3)
    return total, packed


def _solution_from_packed(packed):
    ops = {}
    for j, placed, exits in packed:
        for p in placed:
            en, ex = int(p["entry"]), int(exits[p["id"]])
            ops.setdefault(str(ex), []).append(
                {"type": "EXIT", "block_id": p["id"], "bay_id": j}
            )
            ops.setdefault(str(en), []).append(
                {
                    "type": "ENTRY",
                    "block_id": p["id"],
                    "bay_id": j,
                    "x": int(p["x"]),
                    "y": int(p["y"]),
                    "orient_idx": p["o"],
                }
            )
    for k in ops:
        ops[k].sort(key=lambda d: 0 if d["type"] == "EXIT" else 1)
    return {"operations": {k: ops[k] for k in sorted(ops, key=int)}}


def _solve_covering(prob, pool: ColumnPool, incumbent, deadline, rng: random.Random):
    """Set covering over existing columns, with z[i,j] dedup inside the MIP."""
    if not _HAS_ORTOOLS or time.time() >= deadline:
        return None
    blocks = prob["blocks"]
    bays = prob["bays"]
    n = len(blocks)
    m = len(bays)
    w = prob["weights"]
    cols = list(pool.columns.values())
    if not cols:
        return None

    by_block: list[list[int]] = [[] for _ in range(n)]
    by_bay: list[list[int]] = [[] for _ in range(m)]
    by_block_bay: list[list[list[int]]] = [[[] for _ in range(m)] for _ in range(n)]
    for k, c in enumerate(cols):
        by_bay[c.bay].append(k)
        for i in c.ids:
            by_block[i].append(k)
            by_block_bay[i][c.bay].append(k)
    if any(not by_block[i] for i in range(n)):
        return None

    model = _cp_model.CpModel()
    x = [model.NewBoolVar(f"x{k}") for k in range(len(cols))]
    z = [[model.NewBoolVar(f"z_{i}_{j}") for j in range(m)] for i in range(n)]

    for i in range(n):
        model.Add(sum(x[k] for k in by_block[i]) >= 1)
        model.Add(sum(z[i][j] for j in range(m)) == 1)
        for j in range(m):
            if by_block_bay[i][j]:
                model.Add(z[i][j] <= sum(x[k] for k in by_block_bay[i][j]))
            else:
                model.Add(z[i][j] == 0)
    for j in range(m):
        model.Add(sum(x[k] for k in by_bay[j]) <= 1)

    obj_scale = 100
    load_scale = 100
    obj = []
    for k, c in enumerate(cols):
        # Z1 proxy is attached to selected columns; after dedup this is imperfect,
        # so the final true-score guard remains mandatory.
        obj.append(int(round(obj_scale * w["w1"] * c.tardiness)) * x[k])

    for i, b in enumerate(blocks):
        for j in range(m):
            obj.append(int(round(obj_scale * w["w3"] * _pref_loss(b, j))) * z[i][j])

    if m >= 2:
        areas = [b["width"] * b["height"] for b in bays]
        avg = sum(areas) / m
        norm_load = []
        for j in range(m):
            terms = [
                int(round(load_scale * (avg / areas[j]) * blocks[i]["workload"])) * z[i][j]
                for i in range(n)
            ]
            norm_load.append(sum(terms))
        max_diff = model.NewIntVar(0, 10**12, "z2_scaled")
        for a in range(m):
            for b in range(m):
                if a != b:
                    model.Add(max_diff >= norm_load[a] - norm_load[b])
        z2_coef = max(1, int(round(obj_scale * w["w2"] / load_scale)))
        obj.append(z2_coef * max_diff)

    # Mild duplicate penalty: enough to avoid silly over-covering, not enough to
    # collapse the model back into exact partitioning.
    dup_coef = max(1, int(round(obj_scale * max(w.get("w3", 1.0), 1.0) * 0.02)))
    for i in range(n):
        dup = model.NewIntVar(0, m - 1, f"dup_{i}")
        model.Add(dup == sum(x[k] for k in by_block[i]) - 1)
        obj.append(dup_coef * dup)

    model.Minimize(sum(obj))

    inc_cols = {
        (j, tuple(sorted(i for i, a in incumbent.items() if a == j)))
        for j in range(m)
        if any(a == j for a in incumbent.values())
    }
    for k, c in enumerate(cols):
        model.AddHint(x[k], 1 if (c.bay, c.ids) in inc_cols else 0)
    for i, j0 in incumbent.items():
        for j in range(m):
            model.AddHint(z[i][j], 1 if j == j0 else 0)

    cp = _cp_model.CpSolver()
    cp.parameters.num_search_workers = int(os.environ.get("COVER_CP_WORKERS", "1"))
    cp.parameters.random_seed = rng.randrange(1, 2**30)
    cp_cap = float(os.environ.get("COVER_CP_TIME_CAP", "2.5"))
    cp.parameters.max_time_in_seconds = max(0.2, min(cp_cap, (deadline - time.time()) * 0.35))
    st = cp.Solve(model)
    if st not in (_cp_model.OPTIMAL, _cp_model.FEASIBLE):
        return None

    assign = {}
    for i in range(n):
        for j in range(m):
            if cp.Value(z[i][j]):
                assign[i] = j
                break
    return assign if len(assign) == n else None


def covering_solve(prob, timelimit=60):
    """Entry point for the independent covering-pool prototype."""
    global LAST_STATS
    t0 = time.time()
    safety = max(2.0, timelimit * 0.04)
    build_reserve = max(4.0, timelimit * 0.12)
    deadline = t0 + max(0.0, timelimit - safety - build_reserve)
    poly_deadline = t0 + timelimit - safety

    _clear_kernel_caches()
    rng = random.Random(20260609)
    shadow_rng = random.Random(int(os.environ.get("COVER_SHADOW_SEED", "20260609")))
    pref_rng = random.Random(int(os.environ.get("COVER_PREF_SEED", "20260612")))
    z23_rng = random.Random(int(os.environ.get("COVER_Z23_BRANCH_SEED", "20260613")))
    opts = _fit_options(prob)
    pool = ColumnPool(prob)

    pref_cap_seed = bool(os.environ.get("COVER_PREF_CAP_SEED"))
    z23_seed = os.environ.get("COVER_Z23_SEED", "1") != "0"
    seeds = [
        _seed_pref(prob, opts),
        _seed_balanced(prob, opts),
        _seed_due_balanced(prob, opts),
    ]
    if pref_cap_seed:
        seeds.append(_seed_pref_capped(prob, opts))
    if z23_seed:
        seeds.append(_seed_z23_weighted(prob, opts))
    best = None
    best_score = float("inf")
    shadow_best = None
    shadow_score = float("inf")
    seed_scores = []
    seed_tardiness_min = float("inf")
    archive: list[tuple[float, str, dict[int, int]]] = []
    seen_archive = set()

    def remember(assign, score, source):
        key = _assign_key(assign)
        if key in seen_archive:
            return
        seen_archive.add(key)
        archive.append((score, source, dict(assign)))

    for a in seeds:
        seed_cols = pool.add_assignment(a)
        seed_tardiness_min = min(seed_tardiness_min, sum(c.tardiness for c in seed_cols))
        score = pool.proxy_score(a)
        seed_scores.append(score)
        remember(a, score, "seed")
        if score < best_score:
            best, best_score = dict(a), score
        if score < shadow_score:
            shadow_best, shadow_score = dict(a), score

    # Main loop: covering/dedup candidate, then ILS-like perturbations that add
    # new bay-set columns to the pool.
    no_mip = bool(os.environ.get("COVER_NO_MIP"))
    no_perturb = bool(os.environ.get("COVER_NO_PERTURB"))
    perturbs_per_round = int(os.environ.get("COVER_PERTURBS_PER_ROUND", "6"))
    z23_setting = os.environ.get("COVER_Z23_PERTURBS_PER_ROUND", "auto").lower()
    z23_branch_tardiness_max = None
    z23_auto_count = None
    z23_count6_tardiness_max = None
    z23_count6_w3_max = None
    if z23_setting == "auto":
        z23_branch_tardiness_max = float(os.environ.get("COVER_Z23_BRANCH_TARDINESS_MAX", "250"))
        z23_auto_setting = os.environ.get("COVER_Z23_AUTO_COUNT", "adaptive").lower()
        if z23_auto_setting == "adaptive":
            z23_count6_tardiness_max = float(
                os.environ.get("COVER_Z23_COUNT6_TARDINESS_MAX", "100")
            )
            z23_count6_w3_max = float(os.environ.get("COVER_Z23_COUNT6_W3_MAX", "133"))
            use_count6 = (
                seed_tardiness_min <= z23_count6_tardiness_max
                and float(prob["weights"].get("w3", 0.0)) <= z23_count6_w3_max
            )
            z23_auto_count = 6 if use_count6 else 2
        else:
            z23_auto_count = int(z23_auto_setting)
        z23_perturbs_per_round = (
            z23_auto_count if seed_tardiness_min <= z23_branch_tardiness_max else 0
        )
    else:
        z23_perturbs_per_round = int(z23_setting)
    z23_updates_best = os.environ.get("COVER_Z23_UPDATES_BEST", "1") != "0"
    pref_setting = os.environ.get("COVER_PREF_PERTURBS_PER_ROUND", "auto").lower()
    pref_tardiness_max = None
    pref_w1_max = None
    pref_profile = os.environ.get("COVER_PREF_PROFILE", "wide_guarded").lower()
    pref_guard_low_w1 = None
    pref_guard_tardiness = None
    if pref_setting == "auto":
        # Experimental activation: preference-only moves helped when the problem
        # was already low-tardy and Z1 weight was weak, but hurt hard-tardy cases.
        if pref_profile == "wide_guarded":
            pref_tardiness_max = float(os.environ.get("COVER_PREF_TARDINESS_MAX", "100"))
            pref_w1_max = float(os.environ.get("COVER_PREF_W1_MAX", "30000"))
            pref_guard_low_w1 = float(os.environ.get("COVER_PREF_GUARD_LOW_W1", "11000"))
            pref_guard_tardiness = float(os.environ.get("COVER_PREF_GUARD_TARDINESS", "40"))
        else:
            pref_tardiness_max = float(os.environ.get("COVER_PREF_TARDINESS_MAX", "40"))
            pref_w1_max = float(os.environ.get("COVER_PREF_W1_MAX", "10000"))
        pref_auto_count = int(os.environ.get("COVER_PREF_AUTO_COUNT", "6"))
        w1_value = float(prob["weights"].get("w1", 0.0))
        guarded_ok = (
            pref_profile != "wide_guarded"
            or seed_tardiness_min > pref_guard_tardiness
            or w1_value <= pref_guard_low_w1
        )
        pref_perturbs_per_round = (
            pref_auto_count
            if seed_tardiness_min <= pref_tardiness_max
            and w1_value < pref_w1_max
            and guarded_ok
            else 0
        )
    else:
        pref_auto_count = None
        pref_perturbs_per_round = int(pref_setting)
    pref_updates_best = os.environ.get("COVER_PREF_UPDATES_BEST", "1") != "0"
    shadow_setting = os.environ.get("COVER_SHADOW_PERTURBS_PER_ROUND", "auto").lower()
    if shadow_setting == "auto":
        # Shadow perturb preserves a perturb-only branch.  It helps low/mid-tardiness
        # preference/search cases, but can starve final polygon build on hard
        # tardy cases; gate it by the seed tardiness signal.
        shadow_tardiness_max = float(os.environ.get("COVER_SHADOW_TARDINESS_MAX", "40"))
        shadow_perturbs_per_round = 6 if seed_tardiness_min <= shadow_tardiness_max else 0
    else:
        shadow_tardiness_max = None
        shadow_perturbs_per_round = int(shadow_setting)
    anchor_policy = os.environ.get("COVER_MIP_ANCHOR", "always").lower()
    mip_update_env = os.environ.get("COVER_MIP_UPDATES_BEST")
    mip_update_tardiness_max = float(os.environ.get("COVER_MIP_UPDATE_TARDINESS_MAX", "200"))
    if mip_update_env is None:
        # In hard tardy instances the covering MIP is still useful as an
        # exploration anchor, but its AABB-column proxy can over-commit the
        # incumbent. Let perturb/search own the incumbent there.
        mip_updates_best = seed_tardiness_min <= mip_update_tardiness_max
    else:
        mip_updates_best = mip_update_env != "0"
    max_rounds = int(os.environ.get("COVER_MAX_ROUNDS", "1000000"))
    rounds = 0
    covering_calls = 0
    covering_improvements = 0
    perturb_improvements = 0
    shadow_improvements = 0
    pref_improvements = 0
    z23_improvements = 0
    while time.time() < deadline:
        rounds += 1
        cand = None
        if not no_mip:
            cand = _solve_covering(prob, pool, best, deadline, rng)
            covering_calls += 1
        anchors = [best]
        if cand is not None:
            score = pool.proxy_score(cand)
            improved_by_cover = score < best_score - 1e-9
            remember(cand, score, "cover_improve" if improved_by_cover else "cover")
            if anchor_policy == "always" or (anchor_policy == "improve" and improved_by_cover):
                anchors.append(cand)
            if improved_by_cover and mip_updates_best:
                best, best_score = cand, score
                covering_improvements += 1

        # Generate fresh columns around current promising assignments.
        if not no_perturb:
            if shadow_perturbs_per_round > 0 and shadow_best is not None:
                for _ in range(shadow_perturbs_per_round):
                    if time.time() >= deadline:
                        break
                    strength = shadow_rng.randint(1, 5)
                    trial = _mutate(prob, shadow_best, opts, shadow_rng, strength)
                    score = pool.proxy_score(trial)
                    improved_shadow = score < shadow_score - 1e-9
                    remember(trial, score, "shadow_improve" if improved_shadow else "shadow")
                    if improved_shadow:
                        shadow_best, shadow_score = trial, score
                        shadow_improvements += 1
            if pref_perturbs_per_round > 0:
                pref_anchor = shadow_best if shadow_best is not None else best
                for _ in range(pref_perturbs_per_round):
                    if time.time() >= deadline:
                        break
                    strength = pref_rng.randint(1, 5)
                    trial = _mutate_preference(prob, pref_anchor, opts, pref_rng, strength)
                    score = pool.proxy_score(trial)
                    improved_by_pref = score < best_score - 1e-9
                    remember(trial, score, "pref_improve" if improved_by_pref else "pref")
                    if improved_by_pref and pref_updates_best:
                        best, best_score = trial, score
                        pref_improvements += 1
            if z23_perturbs_per_round > 0:
                z23_anchor = best
                for _ in range(z23_perturbs_per_round):
                    if time.time() >= deadline:
                        break
                    strength = z23_rng.randint(1, 5)
                    trial = _mutate_z23(prob, z23_anchor, opts, z23_rng, strength)
                    score = pool.proxy_score(trial)
                    improved_by_z23 = score < best_score - 1e-9
                    remember(trial, score, "z23_improve" if improved_by_z23 else "z23")
                    if improved_by_z23:
                        z23_anchor = trial
                        z23_improvements += 1
                        if z23_updates_best:
                            best, best_score = trial, score
            for anchor in anchors:
                if time.time() >= deadline:
                    break
                for _ in range(perturbs_per_round):
                    if time.time() >= deadline:
                        break
                    strength = rng.randint(1, 5)
                    trial = _mutate(prob, anchor, opts, rng, strength)
                    score = pool.proxy_score(trial)
                    improved_by_perturb = score < best_score - 1e-9
                    remember(trial, score, "perturb_improve" if improved_by_perturb else "perturb")
                    if improved_by_perturb:
                        best, best_score = trial, score
                        perturb_improvements += 1
        if rounds >= max_rounds:
            break

    search_end = time.time()
    final_checks = 0
    final_build_guard = max(2.0, float(os.environ.get("COVER_FINAL_BUILD_GUARD", "3.0")))
    selection_deadline = poly_deadline - final_build_guard
    final_score_guard = max(0.2, float(os.environ.get("COVER_FINAL_SCORE_GUARD", "0.75")))
    final_score_deadline = poly_deadline - final_score_guard
    final_best = best
    final_obj, final_packed, final_components = _score_and_pack(
        prob, best, deadline=final_score_deadline, with_components=True
    )
    final_source = "search_best"
    final_checks += 1
    # The search proxy is AABB-column based.  Before building the solution, rescore
    # a small portfolio of archive candidates on the same best-of basis used by
    # final build.  Use source buckets as well as proxy rank, so covering does not
    # erase the best perturb-only style path just because its proxy rank is lower.
    elite_n = int(os.environ.get("COVER_FINAL_CANDIDATES", "4"))

    def _portfolio_candidates():
        mode = os.environ.get("COVER_FINAL_PORTFOLIO", "interleave").lower()
        chosen = []
        seen = {_assign_key(final_best)}

        def add(item):
            key = _assign_key(item[2])
            if key not in seen:
                seen.add(key)
                chosen.append(item)

        source_per_group = max(1, int(os.environ.get("COVER_FINAL_SOURCE_PER_GROUP", "1")))

        def add_source_group(sources, start=0, count=1):
            bucket = sorted(
                (item for item in archive if item[1] in sources),
                key=lambda item: item[0],
            )
            for item in bucket[start:start + count]:
                add(item)

        proxy_items = sorted(archive, key=lambda item: item[0])
        if mode == "interleave":
            source_groups = (
                ("z23_improve", "z23"),
                ("perturb_improve", "perturb"),
                ("pref_improve", "pref"),
                ("shadow_improve", "shadow"),
                ("cover_improve", "cover"),
                ("seed",),
            )
            source_extra = int(os.environ.get("COVER_FINAL_SOURCE_CANDIDATES", "4"))
            source_limit = elite_n + source_extra * source_per_group
            for item in proxy_items[:2]:
                add(item)
            for sources in source_groups:
                add_source_group(sources)
                if len(chosen) >= source_limit:
                    break
            for extra_idx in range(1, source_per_group):
                if len(chosen) >= source_limit:
                    break
                for sources in source_groups:
                    add_source_group(sources, start=extra_idx)
                    if len(chosen) >= source_limit:
                        break
            for item in proxy_items:
                if len(chosen) >= source_limit:
                    break
                add(item)
            return chosen[: max(source_limit, 1)]

        if mode != "source":
            for item in proxy_items[:elite_n]:
                add(item)
            if mode != "hybrid":
                return chosen

        source_limit = (
            elite_n
            if mode == "source"
            else elite_n + int(os.environ.get("COVER_FINAL_SOURCE_CANDIDATES", "4"))
        )
        for source in (
            "z23_improve",
            "z23",
            "pref_improve",
            "pref",
            "shadow_improve",
            "shadow",
            "perturb_improve",
            "perturb",
            "cover_improve",
            "cover",
            "seed",
        ):
            bucket = [item for item in archive if item[1] == source]
            if bucket:
                add(min(bucket, key=lambda item: item[0]))
            if len(chosen) >= source_limit:
                break
        for item in proxy_items:
            if len(chosen) >= source_limit:
                break
            add(item)
        return chosen[: max(source_limit, 1)]

    for _, source, cand in _portfolio_candidates():
        if time.time() >= selection_deadline:
            break
        cand_obj, cand_packed, cand_components = _score_and_pack(
            prob, cand, deadline=final_score_deadline, with_components=True
        )
        final_checks += 1
        if cand_obj < final_obj - 1e-9:
            final_best = cand
            final_obj = cand_obj
            final_packed = cand_packed
            final_components = cand_components
            final_source = source

    diag = {}
    source_counts = {}
    for _, source, _ in archive:
        source_counts[source] = source_counts.get(source, 0) + 1
    proxy_top_sources = [
        source for _, source, _ in sorted(archive, key=lambda item: item[0])[:10]
    ]
    diag_n = int(os.environ.get("COVER_DIAG_ARCHIVE", "0"))
    if diag_n > 0:
        # Offline diagnostic only: score the proxy-ranked archive prefix by the
        # true best-of objective to learn whether failures are ranking failures
        # or candidate-generation failures.  It can consume time, so default off.
        diag_items = sorted(archive, key=lambda item: item[0])[:diag_n]
        diag_scores = []
        for rank, (proxy, source, cand) in enumerate(diag_items, start=1):
            if time.time() >= poly_deadline - 1.0:
                break
            obj = _true_obj(prob, cand, deadline=poly_deadline - 1.0)
            diag_scores.append((obj, rank, proxy, source))
        if diag_scores:
            best_diag = min(diag_scores, key=lambda item: item[0])
            diag = {
                "diag_scored": len(diag_scores),
                "diag_best_true": best_diag[0],
                "diag_best_proxy_rank": best_diag[1],
                "diag_best_source": best_diag[3],
                "diag_top_true": diag_scores[0][0],
            }
    if os.environ.get("COVER_DIAG_SOURCE"):
        # Offline diagnostic: score each source bucket's proxy-best candidate by
        # the true objective.  This separates missing-candidate failures from
        # proxy-ranking failures in the covering/archive stage.
        source_priority = (
            "z23_improve",
            "z23",
            "pref_improve",
            "pref",
            "shadow_improve",
            "shadow",
            "perturb_improve",
            "perturb",
            "cover_improve",
            "cover",
            "seed",
        )
        ordered_sources = [
            source for source in source_priority if source in source_counts
        ] + [
            source for source in sorted(source_counts) if source not in source_priority
        ]
        source_limit = int(os.environ.get("COVER_DIAG_SOURCE_LIMIT", "1000000"))
        source_items = []
        for source in ordered_sources[:source_limit]:
            bucket = [item for item in archive if item[1] == source]
            if bucket:
                source_items.append(min(bucket, key=lambda item: item[0]))
        source_scores = []
        for proxy, source, cand in source_items:
            if time.time() >= poly_deadline - 1.0:
                break
            obj = _true_obj(prob, cand, deadline=poly_deadline - 1.0)
            source_scores.append((obj, source, proxy))
        if source_scores:
            best_source = min(source_scores, key=lambda item: item[0])
            diag.update(
                {
                    "diag_source_scored": len(source_scores),
                    "diag_source_best_true": best_source[0],
                    "diag_source_best": best_source[1],
                    "diag_source_scores": source_scores,
                }
            )
    pareto_per_source = int(os.environ.get("COVER_DIAG_SOURCE_PARETO", "0"))
    if pareto_per_source > 0:
        # Diagnostic-only: cheaply scan every archived assignment by AABB-Z1 and
        # exact Z2/Z3, then true-score a small source-wise Pareto slice. This tells
        # us whether low-tardy/high-preference material is absent from the archive
        # or merely missed by final proxy ranking.
        final_z1, final_z2, final_z3 = final_components
        z1_slack = float(os.environ.get("COVER_DIAG_PARETO_Z1_SLACK", "2"))
        source_priority = (
            "z23_improve",
            "z23",
            "pref_improve",
            "pref",
            "shadow_improve",
            "shadow",
            "perturb_improve",
            "perturb",
            "cover_improve",
            "cover",
            "seed",
        )
        source_order = {
            source: idx for idx, source in enumerate(source_priority)
        }
        cheap_rows = []
        for proxy, source, cand in archive:
            cols = pool.add_assignment(cand)
            z1_aabb = sum(c.tardiness for c in cols)
            z2, z3 = _kernel.obj23(prob, cand)
            cheap_rows.append(
                {
                    "proxy": proxy,
                    "source": source,
                    "assign": cand,
                    "z1_aabb": z1_aabb,
                    "z2": z2,
                    "z3": z3,
                }
            )

        promising = [
            row for row in cheap_rows
            if row["z1_aabb"] <= final_z1 + z1_slack and row["z3"] < final_z3
        ]
        by_source = {}
        for row in sorted(
            cheap_rows,
            key=lambda r: (
                source_order.get(r["source"], len(source_order)),
                r["z1_aabb"] > final_z1 + z1_slack,
                r["z3"],
                r["z1_aabb"],
                r["proxy"],
            ),
        ):
            bucket = by_source.setdefault(row["source"], [])
            if len(bucket) < pareto_per_source:
                bucket.append(row)
        diag_items = []
        seen_diag = set()
        for source in sorted(
            by_source,
            key=lambda s: source_order.get(s, len(source_order)),
        ):
            for row in by_source[source]:
                key = _assign_key(row["assign"])
                if key not in seen_diag:
                    seen_diag.add(key)
                    diag_items.append(row)

        diag_rows = []
        for row in diag_items:
            if time.time() >= poly_deadline - 1.0:
                break
            obj, _, comps = _score_and_pack(
                prob,
                row["assign"],
                deadline=poly_deadline - 1.0,
                with_components=True,
            )
            obj1, obj2, obj3 = comps
            diag_rows.append(
                {
                    "source": row["source"],
                    "proxy": row["proxy"],
                    "z1_aabb": row["z1_aabb"],
                    "z2": row["z2"],
                    "z3": row["z3"],
                    "objective": obj,
                    "obj1": obj1,
                    "obj2": obj2,
                    "obj3": obj3,
                }
            )

        true_pref_rows = [
            row for row in diag_rows
            if row["obj1"] <= final_z1 + z1_slack and row["obj3"] < final_z3
        ]
        diag.update(
            {
                "diag_pareto_scanned": len(cheap_rows),
                "diag_pareto_promising": len(promising),
                "diag_pareto_scored": len(diag_rows),
                "diag_pareto_true_pref": len(true_pref_rows),
                "diag_pareto_best_true_pref": (
                    min(true_pref_rows, key=lambda row: row["objective"])
                    if true_pref_rows
                    else None
                ),
                "diag_pareto_rows": diag_rows,
            }
        )

    total_elapsed = time.time() - t0
    search_elapsed = search_end - t0
    LAST_STATS = {
        "elapsed_search": total_elapsed,
        "elapsed_total": total_elapsed,
        "elapsed_search_phase": search_elapsed,
        "elapsed_final_phase": total_elapsed - search_elapsed,
        "search_budget": deadline - t0,
        "poly_budget": poly_deadline - t0,
        "safety": safety,
        "build_reserve": build_reserve,
        "max_rounds": max_rounds,
        "stopped_by_round_cap": rounds >= max_rounds,
        "rounds": rounds,
        "columns": len(pool.columns),
        "covering_calls": covering_calls,
        "covering_improvements": covering_improvements,
        "perturb_improvements": perturb_improvements,
        "shadow_improvements": shadow_improvements,
        "pref_improvements": pref_improvements,
        "z23_improvements": z23_improvements,
        "best_proxy": best_score,
        "shadow_proxy": shadow_score,
        "final_bestof_proxy": final_obj,
        "final_obj1": final_components[0],
        "final_obj2": final_components[1],
        "final_obj3": final_components[2],
        "archive_size": len(archive),
        "archive_source_counts": source_counts,
        "proxy_top_sources": proxy_top_sources,
        "final_checks": final_checks,
        "final_source": final_source,
        "final_build_guard": final_build_guard,
        "final_score_guard": final_score_guard,
        "final_portfolio": os.environ.get("COVER_FINAL_PORTFOLIO", "interleave").lower(),
        "final_source_candidates": int(os.environ.get("COVER_FINAL_SOURCE_CANDIDATES", "4")),
        "final_source_per_group": int(os.environ.get("COVER_FINAL_SOURCE_PER_GROUP", "1")),
        "seed_proxy_min": min(seed_scores) if seed_scores else None,
        "seed_tardiness_min": seed_tardiness_min,
        "shadow_tardiness_max": shadow_tardiness_max,
        "mip_update_tardiness_max": mip_update_tardiness_max,
        "cp_time_cap": float(os.environ.get("COVER_CP_TIME_CAP", "2.5")),
        "pref_cap_seed": pref_cap_seed,
        "z23_seed": z23_seed,
        "no_mip": no_mip,
        "no_perturb": no_perturb,
        "perturbs_per_round": perturbs_per_round,
        "z23_setting": z23_setting,
        "z23_branch_tardiness_max": z23_branch_tardiness_max,
        "z23_auto_count": z23_auto_count,
        "z23_count6_tardiness_max": z23_count6_tardiness_max,
        "z23_count6_w3_max": z23_count6_w3_max,
        "z23_perturbs_per_round": z23_perturbs_per_round,
        "z23_updates_best": z23_updates_best,
        "shadow_perturbs_per_round": shadow_perturbs_per_round,
        "pref_setting": pref_setting,
        "pref_profile": pref_profile,
        "pref_perturbs_per_round": pref_perturbs_per_round,
        "pref_tardiness_max": pref_tardiness_max,
        "pref_w1_max": pref_w1_max,
        "pref_auto_count": pref_auto_count,
        "pref_guard_low_w1": pref_guard_low_w1,
        "pref_guard_tardiness": pref_guard_tardiness,
        "pref_updates_best": pref_updates_best,
        "anchor_policy": anchor_policy,
        "mip_updates_best": mip_updates_best,
        **diag,
    }
    return _solution_from_packed(final_packed)
