"""Self-contained OGC 2026 solver (depends only on utils.py).

Framework: assignment search (scored by a per-bay footprint-disjoint admission
packer) + best-of non-regression guard. Crane extraction is free by construction
because no two temporally-overlapping blocks share a footprint (AABB-disjoint),
which is affordable since bay area utilisation is low. See docs/technical_report.md.

Entry point: framework_solve(prob, timelimit) -> solution dict. Time-managed so it
never exceeds `timelimit`; always returns a feasible solution (disjoint placement
+ guaranteed empty-bay fallback).
"""
from __future__ import annotations
import time
import math
import os
import random
from utils import Bay, Block, check_entry, check_exit

try:
    from shapely.geometry import Polygon as _ShapelyPolygon
    from shapely.ops import unary_union as _unary_union
    from shapely.affinity import translate as _translate
    _HAS_SHAPELY = True
except Exception:                       # pragma: no cover
    _HAS_SHAPELY = False

try:
    from ortools.sat.python import cp_model as _cp_model
    _HAS_ORTOOLS = True
except Exception:                       # pragma: no cover
    _HAS_ORTOOLS = False

# Pool of (bay, block_set) -> AABB tardiness pieces seen during the search, for the
# Z2-aware set-partitioning recombination final step.
_POOL: dict = {}

# Cache of local (reference-anchored) footprints, keyed by (id(block_data), orient).
# A block placed at (x,y) has world footprint = translate(local, x, y), so the
# expensive polygon build happens once per (block, orientation) and translation is
# a cheap affine transform.
_LOCAL_FP: dict = {}

# Cache of the local (origin-anchored) AABB, keyed by (id(block_data), orient).
# A block placed at (x,y) has bounding_rect = this box translated by (x,y) (the
# bbox is translation-equivariant), so the candidate box in the inner packing
# loop is pure arithmetic -- no Block need be built per candidate just to read it.
_LOCAL_BOX: dict = {}


def _local_box(bd, o):
    """Origin-anchored AABB (min_x,min_y,max_x,max_y) for (block, orientation).
    Equals Block(bd, 0, 0, o).bounding_rect(); cached per (block, orientation)."""
    key = (id(bd), o)
    box = _LOCAL_BOX.get(key)
    if box is None:
        box = Block(block_id=-1, block_data=bd, x=0, y=0, orient_idx=o).bounding_rect()
        _LOCAL_BOX[key] = box
    return box

# Search budgeting. By default the searches stop on a wall-clock DEADLINE (used for
# the real submission). For reproducible A/B EVALUATION, set SOLVER_MAX_EVALS: the
# searches then stop after a fixed number of candidate evaluations (total_obj calls)
# -> deterministic, no wall-clock variance. In that mode each `deadline` argument is
# reinterpreted as an eval-count threshold. Either way the harness should report the
# wall-clock time per problem (eval mode does NOT bound time -- it must be watched).
_EVALS = 0
_EVAL_LIMIT = None


def _now():
    return _EVALS if _EVAL_LIMIT is not None else time.time()


def _within(x):
    """True while the search may continue. `x` is a time (default) or, in eval
    mode, an eval-count threshold."""
    return (_EVALS < x) if _EVAL_LIMIT is not None else (time.time() < x)


def _mid(deadline):
    """Halfway point between now and `deadline`, in the active unit (time/evals)."""
    n = _now()
    return n + (deadline - n) * 0.5


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def orient_bbox(bd, o):
    xs = []
    ys = []
    for layer in bd["shape"][o]["layers"]:
        for x, y in layer:
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def fits(bd, bay):
    """An integer-reference placement keeps the block fully inside the bay."""
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        if (math.ceil(max(0.0, -mnx)) + mxx <= bay["width"]
                and math.ceil(max(0.0, -mny)) + mxy <= bay["height"]):
            return True
    return False


# --------------------------------------------------------------------------- #
# per-bay footprint-disjoint admission packer
# --------------------------------------------------------------------------- #
def find_slot(bay, present_objs, overlap_objs, bd, bid, W, H, step):
    """Earliest bottom-left feasible (x, y, o): in-bounds, crane-entry OK, and
    full-footprint (AABB) disjoint from all temporally-overlapping blocks
    (=> no cross-layer overlap => never crane-trapped)."""
    ov_boxes = [ob.bounding_rect() for ob in overlap_objs]
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        # integer range: lower=ceil (else min vertex < 0), upper=floor (else > W)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        # candidate box = origin box translated by (x, y); arithmetic only, so we
        # build a Block only for the few candidates that survive the overlap reject
        # and need the (boundary) crane-entry check.
        lbx0, lby0, lbx1, lby1 = _local_box(bd, o)
        for y in range(y_start, y_end + 1, step):
            cy0 = lby0 + y
            cy1 = lby1 + y
            for x in range(x_start, x_end + 1, step):
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                if any(cx0 < b[2] and b[0] < cx1 and cy0 < b[3] and b[1] < cy1
                       for b in ov_boxes):
                    continue
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                if check_entry(bay, present_objs, cand):  # boundary only now
                    continue
                return (x, y, o)
    return None


def _local_footprint(bd, o):
    """Footprint of the block at orientation o anchored at (0,0); cached so the
    Shapely build runs once per (block, orientation)."""
    key = (id(bd), o)
    fp = _LOCAL_FP.get(key, 0)
    if fp != 0:
        return fp
    polys = []
    for layer in Block(block_id=-1, block_data=bd, x=0, y=0, orient_idx=o).layers_at_pos():
        if len(layer) >= 3:
            p = _ShapelyPolygon(layer)
            if not p.is_valid:
                p = p.buffer(0)
            polys.append(p)
    fp = _unary_union(polys) if polys else None
    _LOCAL_FP[key] = fp
    return fp


def _block_footprint(bd, x, y, o):
    """World footprint = local footprint translated by (x, y) (cheap affine)."""
    loc = _local_footprint(bd, o)
    return _translate(loc, x, y) if loc is not None else None


def find_slot_poly(bay, present_objs, ov_boxfps, bd, bid, W, H, step):
    """Like find_slot but disjointness is the EXACT footprint (polygon), which is
    strictly more permissive than AABB (packs tighter). AABB is used only as a
    cheap pre-filter. `ov_boxfps` = precomputed [(bbox, footprint)] of the
    temporally-overlapping placed blocks (footprints cached, not rebuilt here)."""
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        lbx0, lby0, lbx1, lby1 = _local_box(bd, o)  # candidate box = this + (x, y)
        for y in range(y_start, y_end + 1, step):
            cy0 = lby0 + y
            cy1 = lby1 + y
            for x in range(x_start, x_end + 1, step):
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                cfp = None
                bad = False
                for (ob_box, ob_fp) in ov_boxfps:
                    if not (cx0 < ob_box[2] and ob_box[0] < cx1
                            and cy0 < ob_box[3] and ob_box[1] < cy1):
                        continue  # AABBs disjoint => footprints disjoint
                    if cfp is None:
                        cfp = _block_footprint(bd, x, y, o)
                    if cfp is not None and ob_fp is not None and cfp.intersection(ob_fp).area > 1e-9:
                        bad = True
                        break
                if bad:
                    continue
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                if check_entry(bay, present_objs, cand):
                    continue
                return (x, y, o)
    return None


def solve_bay(prob, j, ids, step=2, tcap=200, poly=False, deadline=None):
    """Per-bay footprint-disjoint admission packer. If poly=True, escalate to the
    exact polygon-disjoint check whenever cheap AABB finds no slot at a time t
    (recovers packing-driven tardiness; pays the shapely cost only where needed).
    Once `deadline` (wall-clock) passes, poly escalation is dropped so the pack
    always finishes in bounded time (AABB-only is the validated-feasible floor)."""
    use_poly = poly and _HAS_SHAPELY
    bays = prob["bays"]
    blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j)
    W = bays[j]["width"]
    H = bays[j]["height"]
    placed = []
    order = sorted(ids, key=lambda i: (blocks[i]["due_date"], blocks[i]["processing_time"]))
    for i in order:
        bd = blocks[i]
        R = bd["release_time"]
        P = bd["processing_time"]
        chosen = None
        for t in range(R, R + tcap):
            present = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                       for p in placed if p["entry"] <= t < p["exit"]]
            overlap = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                       for p in placed if p["entry"] < t + P and t < p["exit"]]
            slot = find_slot(bay, present, overlap, bd, i, W, H, step)
            if slot is None and use_poly:
                if deadline is not None and time.time() > deadline:
                    use_poly = False  # out of time: revert to AABB for the rest
                else:
                    ov_boxfps = [(p["bb"], p["fp"]) for p in placed
                                 if p["entry"] < t + P and t < p["exit"]]
                    slot = find_slot_poly(bay, present, ov_boxfps, bd, i, W, H, step)
            if slot:
                chosen = (t, slot[0], slot[1], slot[2])
                break
        if chosen is None:
            # fallback: search forward from when the bay empties, then a fitting
            # orientation at its in-bounds corner (boundary + collision safe).
            t = max((p["exit"] for p in placed), default=R)
            for tt in range(t, t + 1000):
                present = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                           for p in placed if p["entry"] <= tt < p["exit"]]
                overlap = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                           for p in placed if p["entry"] < tt + P and tt < p["exit"]]
                slot = find_slot(bay, present, overlap, bd, i, W, H, step)
                if slot:
                    chosen = (tt, slot[0], slot[1], slot[2])
                    break
            if chosen is None:
                o_fit = next((o for o in range(len(bd["shape"]))
                              if math.ceil(max(0.0, -orient_bbox(bd, o)[0])) + orient_bbox(bd, o)[2] <= W
                              and math.ceil(max(0.0, -orient_bbox(bd, o)[1])) + orient_bbox(bd, o)[3] <= H), 0)
                mnx, mny, _, _ = orient_bbox(bd, o_fit)
                chosen = (t, math.ceil(max(0.0, -mnx)), math.ceil(max(0.0, -mny)), o_fit)
        rec = {"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
               "entry": chosen[0], "exit": chosen[0] + P}
        if use_poly:  # cache footprint + bbox so later poly checks never rebuild
            blk_o = Block(i, bd, chosen[1], chosen[2], chosen[3])
            rec["bb"] = blk_o.bounding_rect()
            rec["fp"] = _block_footprint(bd, chosen[1], chosen[2], chosen[3])
        placed.append(rec)
    return placed


def extract_tardiness(prob, j, placed):
    """Greedy ASAP extraction (exit when processing done + crane-untrapped)."""
    bays = prob["bays"]
    blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j)
    blk = {p["id"]: Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"]) for p in placed}
    entry = {p["id"]: p["entry"] for p in placed}
    proc = {p["id"]: blocks[p["id"]]["processing_time"] for p in placed}
    due = {p["id"]: blocks[p["id"]]["due_date"] for p in placed}
    ids = list(blk)
    horizon = max(entry[i] + proc[i] for i in ids) + len(ids) + 5
    present = set()
    exited = {}
    pend = sorted(ids, key=lambda i: entry[i])
    pe = 0
    for t in range(0, horizon + 1):
        changed = True
        while changed:
            changed = False
            for i in list(present):
                if t >= entry[i] + proc[i]:
                    if not check_exit(bay, [blk[k] for k in present], blk[i]):
                        exited[i] = t
                        present.discard(i)
                        changed = True
        while pe < len(pend) and entry[pend[pe]] <= t:
            present.add(pend[pe])
            pe += 1
        if not present and pe >= len(pend):
            break
    for i in ids:
        exited.setdefault(i, horizon)
    tard = sum(max(0.0, exited[i] - due[i]) for i in ids)
    return tard, exited


# --------------------------------------------------------------------------- #
# objective evaluation (Z2/Z3 instant; Z1 via per-bay packing, cached)
# --------------------------------------------------------------------------- #
def obj23(prob, assign):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    loads = [0.0] * m
    obj3 = 0.0
    for i, j in assign.items():
        loads[j] += blocks[i]["workload"]
        obj3 += max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    obj2 = math.floor(max((abs(u[a] * loads[a] - u[b] * loads[b])
                           for a in range(m) for b in range(m) if a != b), default=0.0))
    return obj2, obj3


def eval_obj1(prob, assign, cache):
    m = len(prob["bays"])
    obj1 = 0.0
    perbay = {}
    for j in range(m):
        ids = tuple(sorted(i for i, a in assign.items() if a == j))
        if not ids:
            perbay[j] = 0.0
            continue
        if ids in cache:
            perbay[j] = cache[ids]
        else:
            T, _ = extract_tardiness(prob, j, solve_bay(prob, j, list(ids)))
            cache[ids] = T
            perbay[j] = T
        _POOL[(j, ids)] = perbay[j]   # record (bay, set) piece for recombination
        obj1 += perbay[j]
    return obj1, perbay


def total_obj(prob, assign, cache):
    global _EVALS
    _EVALS += 1
    obj1, perbay = eval_obj1(prob, assign, cache)
    obj2, obj3 = obj23(prob, assign)
    w = prob["weights"]
    tot = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    return tot, perbay


# --------------------------------------------------------------------------- #
# assignment heuristics
# --------------------------------------------------------------------------- #
def a_pref(prob):
    blocks = prob["blocks"]
    bays = prob["bays"]
    asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        asg[i] = next((j for j in order if fits(b, bays[j])), order[0])
    return asg


def a_balanced_load(prob):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    loads = [0.0] * m
    asg = {}
    for i in sorted(range(len(blocks)), key=lambda i: -blocks[i]["workload"]):
        b = blocks[i]
        cand = [j for j in range(m) if fits(b, bays[j])] or list(range(m))
        j = min(cand, key=lambda j: u[j] * (loads[j] + b["workload"]))
        asg[i] = j
        loads[j] += b["workload"]
    return asg


def a_pref_capped(prob, cap_factor=1.15):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    cap = math.ceil(len(blocks) / m * cap_factor)
    cnt = [0] * m
    asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        order = [j for j in order if fits(b, bays[j])] or order
        j = next((j for j in order if cnt[j] < cap), order[0])
        asg[i] = j
        cnt[j] += 1
    return asg


# --------------------------------------------------------------------------- #
# searches (deadline-driven; only accept improving moves)
# --------------------------------------------------------------------------- #
def local_search(prob, assign, cache, deadline):
    """Focused hill climb: move blocks out of tardy bays (first improvement)."""
    m = len(prob["bays"])
    blocks = prob["blocks"]
    best = dict(assign)
    best_tot, perbay = total_obj(prob, best, cache)
    improved = True
    while improved and _within(deadline):
        improved = False
        tardy = [j for j in range(m) if perbay.get(j, 0) > 0]
        movers = [i for i in best if best[i] in tardy]
        for i in movers:
            if not _within(deadline):
                break
            for j in range(m):
                if j == best[i] or not fits(blocks[i], prob["bays"][j]):
                    continue
                trial = dict(best)
                trial[i] = j
                tot, _ = total_obj(prob, trial, cache)
                if tot < best_tot - 1e-9:
                    best, best_tot = trial, tot
                    _, perbay = total_obj(prob, best, cache)
                    improved = True
                    break
    return best, best_tot


def improved_search(prob, cache, deadline):
    """Two-phase hill climb from the best heuristic seed. Phase 1 (first half):
    movers in tardy bays (Z1) + the max-(u*load) bay (Z2). Phase 2: also move
    blocks off their preferred bay (Z3), tried preference-first."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur, cur_tot = None, float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob)
        tot, _ = total_obj(prob, a, cache)
        if tot < cur_tot:
            cur, cur_tot = dict(a), tot
    _, perbay = total_obj(prob, cur, cache)

    def hillclimb(include_offpref, sub_deadline):
        nonlocal cur, cur_tot, perbay
        improved = True
        while improved and _within(sub_deadline):
            improved = False
            loads = [0.0] * m
            for i, j in cur.items():
                loads[j] += blocks[i]["workload"]
            maxload = max(range(m), key=lambda j: u[j] * loads[j])
            tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
            movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                      or (include_offpref and cur[i] != pref_bay[i])]
            for i in movers:
                if not _within(sub_deadline):
                    break
                targets = sorted((j for j in range(m)
                                  if j != cur[i] and fits(blocks[i], bays[j])),
                                 key=lambda j: -blocks[i]["bay_preferences"][j])
                for j in targets:
                    trial = dict(cur)
                    trial[i] = j
                    tot, _ = total_obj(prob, trial, cache)
                    if tot < cur_tot - 1e-9:
                        cur, cur_tot = trial, tot
                        _, perbay = total_obj(prob, cur, cache)
                        improved = True
                        break

    hillclimb(False, _mid(deadline))
    hillclimb(True, deadline)
    return cur, cur_tot


def _climb(prob, assign, cache, deadline):
    """Hill climb to convergence-or-deadline: move blocks in tardy bays (Z1), the
    max-(u*load) bay (Z2), or off their preferred bay (Z3); targets preference-first."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur = dict(assign)
    cur_tot, perbay = total_obj(prob, cur, cache)
    improved = True
    while improved and _within(deadline):
        improved = False
        loads = [0.0] * m
        for i, j in cur.items():
            loads[j] += blocks[i]["workload"]
        maxload = max(range(m), key=lambda j: u[j] * loads[j])
        tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
        movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                  or cur[i] != pref_bay[i]]
        for i in movers:
            if not _within(deadline):
                break
            targets = sorted((j for j in range(m)
                              if j != cur[i] and fits(blocks[i], bays[j])),
                             key=lambda j: -blocks[i]["bay_preferences"][j])
            for j in targets:
                trial = dict(cur)
                trial[i] = j
                tot, _ = total_obj(prob, trial, cache)
                if tot < cur_tot - 1e-9:
                    cur, cur_tot = trial, tot
                    _, perbay = total_obj(prob, cur, cache)
                    improved = True
                    break
    return cur, cur_tot


def _ils(prob, best, best_tot, cache, deadline, rng):
    """Iterated local search on the IDLE budget left after the main search converges.
    Perturb the incumbent by re-homing a few blocks, re-optimise, keep the global
    best. Default destroy is random; SOLVER_GUIDED=1 destroys *contributing* blocks
    (in a tardy bay [Z1], the max-(u*load) bay [Z2], or off their preferred bay
    [Z3]) -- repair stays randomized for diversity."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    ids = list(best)
    guided = os.environ.get("SOLVER_GUIDED")   # "1"=always guided, "mix"=50/50
    if guided:
        areas = [b["width"] * b["height"] for b in bays]
        u = [sum(areas) / m / a for a in areas]
        pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                    for i in range(len(blocks))}
    while _within(deadline):
        k = rng.randint(2, 5)
        cand = dict(best)
        use_g = guided and (guided != "mix" or rng.random() < 0.5)
        if use_g:
            _, perbay = total_obj(prob, best, cache)
            loads = [0.0] * m
            for i, j in best.items():
                loads[j] += blocks[i]["workload"]
            maxload = max(range(m), key=lambda j: u[j] * loads[j])
            tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
            pool = [i for i in ids if best[i] in tardy or best[i] == maxload
                    or best[i] != pref_bay[i]]
            chosen = rng.sample(pool, min(k, len(pool))) if pool else rng.sample(ids, min(k, len(ids)))
        else:
            chosen = [rng.choice(ids) for _ in range(k)]
        for i in chosen:
            opts = [j for j in range(m) if j != cand[i] and fits(blocks[i], bays[j])]
            if opts:
                cand[i] = rng.choice(opts)
        cur, tot = _climb(prob, cand, cache, deadline)
        if tot < best_tot - 1e-9:
            best, best_tot = cur, tot
    return best, best_tot


def _bestof_obj(prob, assign, deadline=None):
    """Full objective on the same per-bay best-of(AABB, polygon) basis the final
    build uses: w1·Σ min(T_aabb,T_poly) + w2·Z2 + w3·Z3. Used to guard the
    recombination adoption (Pareto-safe)."""
    w = prob["weights"]
    m = len(prob["bays"])
    o1 = 0.0
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        Ta, _ = extract_tardiness(prob, j, solve_bay(prob, j, ids, poly=False))
        Tp, _ = extract_tardiness(prob, j, solve_bay(prob, j, ids, poly=True, deadline=deadline))
        o1 += min(Ta, Tp)
    o2, o3 = obj23(prob, assign)
    return w["w1"] * o1 + w["w2"] * o2 + w["w3"] * o3


def _recombine(prob, best, deadline):
    """Z2-aware set-partitioning recombination of the cached (bay,set) pieces.
    Local search moves one block at a time and cannot recombine whole bay-pieces
    across solutions; the MIP can. column cost = w1·tardiness + w3·preference,
    global term = w2·Z2 (min-max, linearized). Hinted with the incumbent (so a
    time cut-off still yields >= incumbent on the MIP metric) and adopted only if
    the full best-of objective actually improves -- Pareto-safe."""
    if not _HAS_ORTOOLS:
        return best
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    n = len(blocks)
    w = prob["weights"]
    SCALE = 100
    cols = [(j, ids, T) for (j, ids), T in _POOL.items() if ids]
    if not cols:
        return best
    model = _cp_model.CpModel()
    x = [model.NewBoolVar(f"c{k}") for k in range(len(cols))]
    by_block = [[] for _ in range(n)]
    by_bay = [[] for _ in range(m)]
    wl = []
    cost = []
    for k, (j, ids, T) in enumerate(cols):
        for i in ids:
            by_block[i].append(k)
        by_bay[j].append(k)
        wl.append(sum(blocks[i]["workload"] for i in ids))
        pl = sum(max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j] for i in ids)
        cost.append(int(SCALE * (w["w1"] * T + w["w3"] * pl)))
    for i in range(n):
        if not by_block[i]:
            return best
        model.Add(sum(x[k] for k in by_block[i]) == 1)
    for j in range(m):
        if by_bay[j]:
            model.Add(sum(x[k] for k in by_bay[j]) <= 1)
    obj = sum(x[k] * cost[k] for k in range(len(cols)))
    if m >= 2:
        areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
        cj = [round(avg / areas[j] * SCALE) for j in range(m)]
        sload = [sum(cj[j] * wl[k] * x[k] for k in by_bay[j]) for j in range(m)]
        M = model.NewIntVar(0, 10 ** 12, "M")     # = SCALE * Z2
        for a in range(m):
            for b in range(m):
                if a != b:
                    model.Add(M >= sload[a] - sload[b])
        obj = obj + w["w2"] * M
    model.Minimize(obj)
    inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
           for j in range(m) if any(best[i] == j for i in best)}
    for k, (j, ids, T) in enumerate(cols):
        model.AddHint(x[k], 1 if (j, ids) in inc else 0)
    cp = _cp_model.CpSolver()
    cp.parameters.max_time_in_seconds = max(0.5, (deadline - time.time()) * 0.5)
    cp.parameters.num_search_workers = 4
    st = cp.Solve(model)
    if st not in (_cp_model.OPTIMAL, _cp_model.FEASIBLE):
        return best
    A = {}
    for k, (j, ids, T) in enumerate(cols):
        if cp.Value(x[k]) == 1:
            for i in ids:
                A[i] = j
    if len(A) != n:
        return best
    # best-of full-objective guard (Pareto-safe): adopt only if A truly improves.
    if _bestof_obj(prob, A, deadline) < _bestof_obj(prob, best, deadline) - 1e-9:
        return A
    return best


# --------------------------------------------------------------------------- #
# solution assembly + top-level solve
# --------------------------------------------------------------------------- #
def build_solution(prob, assign, poly_deadline=None):
    m = len(prob["bays"])
    ops = {}
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        # Always pack AABB; ALSO pack with polygon escalation when there's time,
        # and keep whichever gives lower tardiness. Polygon packing is usually
        # better (recovers packing-driven tardiness) but is greedy -- placing one
        # block earlier can push others out -- so it can occasionally be WORSE than
        # AABB; best-of guarantees we never lose to the AABB packing.
        # SOLVER_NOPOLY=1 forces AABB-only (ablation baseline).
        placed = solve_bay(prob, j, ids, poly=False)
        T_aabb, exits = extract_tardiness(prob, j, placed)
        use_poly = ((poly_deadline is None or time.time() < poly_deadline)
                    and not os.environ.get("SOLVER_NOPOLY"))
        if use_poly:
            placed_p = solve_bay(prob, j, ids, poly=True, deadline=poly_deadline)
            T_poly, exits_p = extract_tardiness(prob, j, placed_p)
            if T_poly < T_aabb:
                placed, exits = placed_p, exits_p
        for p in placed:
            en, ex = int(p["entry"]), int(exits[p["id"]])
            ops.setdefault(str(ex), []).append({"type": "EXIT", "block_id": p["id"], "bay_id": j})
            ops.setdefault(str(en), []).append({"type": "ENTRY", "block_id": p["id"], "bay_id": j,
                                                "x": int(p["x"]), "y": int(p["y"]), "orient_idx": p["o"]})
    for k in ops:
        ops[k].sort(key=lambda d: 0 if d["type"] == "EXIT" else 1)
    # time keys in sorted order: check_feasibility reconstructs in insertion order,
    # and an EXIT key before its ENTRY key would lose the exit_time.
    ops = {k: ops[k] for k in sorted(ops, key=int)}
    return {"operations": ops}


def framework_solve(prob, timelimit):
    """Time-managed solve. Reserves a build margin, splits the rest between the
    two-phase search and the focused search (best-of, shared cache), and always
    returns a feasible solution."""
    global _EVALS, _EVAL_LIMIT
    _EVALS = 0
    _POOL.clear()
    # These caches are keyed by id(block_data); a later instance's block dict can
    # reuse a freed address, so a stale entry would corrupt packing if the module
    # is reused across problems (multi-instance harness/benchmark). Production runs
    # one problem per process, but clearing per solve makes reuse safe and is cheap.
    _LOCAL_FP.clear()
    _LOCAL_BOX.clear()
    t0 = time.time()
    cache = {}
    safety = max(2.0, timelimit * 0.04)
    recomb_on = _HAS_ORTOOLS and not os.environ.get("SOLVER_NORECOMB")

    # EVALUATION mode (reproducible, deterministic): stop the searches by candidate-
    # evaluation count, run the full polygon build, and let the harness report wall
    # time. Default (submission) mode is wall-clock time-managed.
    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    _EVAL_LIMIT = int(max_evals) if max_evals else None

    # best heuristic as a guaranteed feasible floor; also detect whether tardiness
    # is even in play (min obj1 over seeds).
    best_seed, bt, min_o1 = None, float("inf"), float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob)
        tot, perbay = total_obj(prob, a, cache)
        min_o1 = min(min_o1, sum(perbay.values()))
        if tot < bt:
            bt, best_seed = tot, a
    best, best_tot = best_seed, bt

    recomb_deadline = None
    if _EVAL_LIMIT is not None:
        # eval-count thresholds (cumulative): improved 40%, local 70%, ILS 100%.
        imp_dl, bas_dl, ils_dl = _EVAL_LIMIT * 0.4, _EVAL_LIMIT * 0.7, _EVAL_LIMIT
        poly_deadline = None    # full deterministic polygon build
        if recomb_on:
            recomb_deadline = None   # set just before the call (wall-clock cap)
    else:
        # The exact polygon final-build only recovers PACKING-driven tardiness, so
        # reserve time for it only when tardiness is in play; preference-only
        # instances (a seed already reaches obj1=0) give the budget to search.
        poly_build_reserve = (max(6.0, timelimit * 0.30) if min_o1 > 1e-9
                              else max(1.0, timelimit * 0.04))
        # the recombination (MIP + best-of guard) needs ~2 builds' worth of time
        recombine_reserve = max(5.0, timelimit * 0.18) if recomb_on else 0.0
        search_total = max(0.0, timelimit - poly_build_reserve - recombine_reserve - safety)
        imp_dl = t0 + search_total * 0.5
        bas_dl = ils_dl = t0 + search_total
        recomb_deadline = t0 + search_total + recombine_reserve
        poly_deadline = t0 + timelimit - safety

    try:
        asg_imp, t_imp = improved_search(prob, cache, deadline=imp_dl)
        if t_imp < best_tot:
            best, best_tot = asg_imp, t_imp
        asg_bas, t_bas = local_search(prob, best_seed, cache, deadline=bas_dl)
        if t_bas < best_tot:
            best, best_tot = asg_bas, t_bas
        # iterated local search on whatever budget the main search left unused
        # (SOLVER_NOILS=1 disables it -- ablation baseline)
        rng = random.Random(0)
        do_ils = not os.environ.get("SOLVER_NOILS")
        # H2 (search -> recombine -> search loop) was tested and rejected: splitting the
        # ILS budget to recombine mid-search commits the assignment to a basin built from
        # a thin pool, then burns the rest re-searching there. Deterministic E=2000 A/B
        # vs the single final recombine: prob_13 +9.9%, prob_17 +57.7%, prob_5 0% -- worse
        # everywhere, no winning instance. One uninterrupted ILS + one final recombine of
        # the rich pool is strictly better. See docs/experiment_log.md.
        if do_ils:
            best, best_tot = _ils(prob, best, best_tot, cache, deadline=ils_dl, rng=rng)
        # Z2-aware set-partitioning recombination of the cached pieces (guarded;
        # adopted only if the full best-of objective improves). SOLVER_NORECOMB=1
        # disables it.
        if recomb_on:
            rdl = recomb_deadline if recomb_deadline is not None else (time.time() + 30.0)
            best = _recombine(prob, best, deadline=rdl)
    except Exception:
        pass  # keep the best feasible assignment found so far

    return build_solution(prob, best, poly_deadline=poly_deadline)
