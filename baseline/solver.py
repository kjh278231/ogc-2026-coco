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
from utils import Bay, Block, check_entry, check_exit


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
        for y in range(y_start, y_end + 1, step):
            for x in range(x_start, x_end + 1, step):
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                cb = cand.bounding_rect()
                if any(cb[0] < b[2] and b[0] < cb[2] and cb[1] < b[3] and b[1] < cb[3]
                       for b in ov_boxes):
                    continue
                if check_entry(bay, present_objs, cand):  # boundary only now
                    continue
                return (x, y, o)
    return None


def solve_bay(prob, j, ids, step=2, tcap=200):
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
        placed.append({"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
                       "entry": chosen[0], "exit": chosen[0] + P})
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
        obj1 += perbay[j]
    return obj1, perbay


def total_obj(prob, assign, cache):
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
    while improved and time.time() < deadline:
        improved = False
        tardy = [j for j in range(m) if perbay.get(j, 0) > 0]
        movers = [i for i in best if best[i] in tardy]
        for i in movers:
            if time.time() >= deadline:
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
    t0 = time.time()
    span = max(0.0, deadline - t0)
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
        while improved and time.time() < sub_deadline:
            improved = False
            loads = [0.0] * m
            for i, j in cur.items():
                loads[j] += blocks[i]["workload"]
            maxload = max(range(m), key=lambda j: u[j] * loads[j])
            tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
            movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                      or (include_offpref and cur[i] != pref_bay[i])]
            for i in movers:
                if time.time() >= sub_deadline:
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

    hillclimb(False, t0 + span * 0.5)
    hillclimb(True, deadline)
    return cur, cur_tot


# --------------------------------------------------------------------------- #
# solution assembly + top-level solve
# --------------------------------------------------------------------------- #
def build_solution(prob, assign):
    m = len(prob["bays"])
    ops = {}
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        placed = solve_bay(prob, j, ids)
        _, exits = extract_tardiness(prob, j, placed)
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
    t0 = time.time()
    cache = {}
    build_margin = max(4.0, timelimit * 0.12)   # final assembly re-packs all bays
    overrun = 2.0                                # one bay-pack can overshoot a check
    search_total = max(0.0, timelimit - build_margin - overrun)
    half = search_total * 0.5

    # best heuristic as a guaranteed feasible floor
    best_seed, bt = None, float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob)
        tot, _ = total_obj(prob, a, cache)
        if tot < bt:
            bt, best_seed = tot, a
    best, best_tot = best_seed, bt

    try:
        asg_imp, t_imp = improved_search(prob, cache, deadline=t0 + half)
        if t_imp < best_tot:
            best, best_tot = asg_imp, t_imp
        asg_bas, t_bas = local_search(prob, best_seed, cache, deadline=t0 + search_total)
        if t_bas < best_tot:
            best, best_tot = asg_bas, t_bas
    except Exception:
        pass  # keep the best feasible assignment found so far

    return build_solution(prob, best)
