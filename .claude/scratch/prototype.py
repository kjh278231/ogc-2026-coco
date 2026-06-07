"""First framework prototype: per-bay verification-guided ADMISSION policy.

Fixed assignment (taken from baseline so we isolate admission-policy quality).
For each block (EDD order) we actively SEARCH for the earliest feasible entry
slot, preferring on-time, packing bottom-left to preserve contiguous free space
for future arrivals.  Extraction is free (Exp A) so exit = entry + proc, with a
final greedy extraction sim to resolve any residual traps.

Compares per-bay tardiness:  baseline  vs  this prototype  (same assignment).
"""
import sys, os, json, time, math
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline"))
import baseline_greedy, utils
from utils import Bay, Block, check_entry, check_exit, check_collisions


def orient_bbox(bd, o):
    xs = []; ys = []
    for layer in bd["shape"][o]["layers"]:
        for x, y in layer:
            xs.append(x); ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def find_slot(bay, present_objs, overlap_objs, bd, bid, W, H, step):
    """Earliest-found bottom-left feasible (x,y,o) or None.
    Feasible = bay-contained + crane-entry OK vs present + collision-free vs all
    temporally-overlapping placed blocks."""
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        # valid integer range so block stays in [0,W]x[0,H]:
        # lower bound ceil (else min vertex < 0), upper bound floor (else max > W)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        ov_boxes = [ob.bounding_rect() for ob in overlap_objs]
        for y in range(y_start, y_end + 1, step):       # bottom first
            for x in range(x_start, x_end + 1, step):   # left first
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                cb = cand.bounding_rect()
                # EXTRACTABILITY GUARANTEE: full-footprint AABB disjoint from all
                # temporally-overlapping blocks => no cross-layer overlap => no crane
                # trap ever. Affordable because area utilisation is only ~0.3-0.4.
                if any(cb[0] < b[2] and b[0] < cb[2] and cb[1] < b[3] and b[1] < cb[3]
                       for b in ov_boxes):
                    continue
                if check_entry(bay, present_objs, cand):  # boundary only now
                    continue
                return (x, y, o)
    return None


def solve_bay(prob, j, ids, step=2, tcap=200):
    bays = prob["bays"]; blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j); W = bays[j]["width"]; H = bays[j]["height"]
    placed = []  # dict id,x,y,o,entry,exit
    order = sorted(ids, key=lambda i: (blocks[i]["due_date"], blocks[i]["processing_time"]))
    for i in order:
        bd = blocks[i]; R = bd["release_time"]; P = bd["processing_time"]
        chosen = None
        for t in range(R, R + tcap):
            present = [Block(p["id"], blocks[p["id"]], int(round(p["x"])), int(round(p["y"])), p["o"])
                       for p in placed if p["entry"] <= t < p["exit"]]
            overlap = [Block(p["id"], blocks[p["id"]], int(round(p["x"])), int(round(p["y"])), p["o"])
                       for p in placed if p["entry"] < t + P and t < p["exit"]]
            slot = find_slot(bay, present, overlap, bd, i, W, H, step)
            if slot:
                chosen = (t, slot[0], slot[1], slot[2]); break
        if chosen is None:
            # robust fallback: search forward from when the bay empties, where a
            # fitting orientation is guaranteed a valid (in-bounds, collision-free)
            # slot. Never place blindly (that caused out-of-bounds Stage-2 fails).
            t = max((p["exit"] for p in placed), default=R)
            for tt in range(t, t + 1000):
                present = [Block(p["id"], blocks[p["id"]], int(round(p["x"])), int(round(p["y"])), p["o"])
                           for p in placed if p["entry"] <= tt < p["exit"]]
                overlap = [Block(p["id"], blocks[p["id"]], int(round(p["x"])), int(round(p["y"])), p["o"])
                           for p in placed if p["entry"] < tt + P and tt < p["exit"]]
                slot = find_slot(bay, present, overlap, bd, i, W, H, step)
                if slot:
                    chosen = (tt, slot[0], slot[1], slot[2]); break
            if chosen is None:
                # deterministic last resort: a fitting orientation at its in-bounds
                # corner, at bay-empty time -> guaranteed boundary + collision safe.
                o_fit = next((o for o in range(len(bd["shape"]))
                              if math.ceil(max(0.0, -orient_bbox(bd, o)[0])) + orient_bbox(bd, o)[2] <= W
                              and math.ceil(max(0.0, -orient_bbox(bd, o)[1])) + orient_bbox(bd, o)[3] <= H), 0)
                mnx, mny, _, _ = orient_bbox(bd, o_fit)
                chosen = (t, math.ceil(max(0.0, -mnx)), math.ceil(max(0.0, -mny)), o_fit)
            solve_bay.n_fallback += 1
        placed.append({"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
                       "entry": chosen[0], "exit": chosen[0] + P})
    return placed
solve_bay.n_fallback = 0


def extract_tardiness(prob, j, placed):
    """Greedy ASAP extraction (exit when done + crane-untrapped). Returns tardiness."""
    bays = prob["bays"]; blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j)
    blk = {p["id"]: Block(p["id"], blocks[p["id"]], int(round(p["x"])), int(round(p["y"])), p["o"])
           for p in placed}
    entry = {p["id"]: p["entry"] for p in placed}
    proc = {p["id"]: blocks[p["id"]]["processing_time"] for p in placed}
    due = {p["id"]: blocks[p["id"]]["due_date"] for p in placed}
    ids = list(blk)
    horizon = max(entry[i] + proc[i] for i in ids) + len(ids) + 5
    present = set(); exited = {}
    pend = sorted(ids, key=lambda i: entry[i]); pe = 0
    for t in range(0, horizon + 1):
        changed = True
        while changed:
            changed = False
            for i in list(present):
                if t >= entry[i] + proc[i]:
                    if not check_exit(bay, [blk[k] for k in present], blk[i]):
                        exited[i] = t; present.discard(i); changed = True
        while pe < len(pend) and entry[pend[pe]] <= t:
            present.add(pend[pe]); pe += 1
        if not present and pe >= len(pend):
            break
    for i in ids:
        exited.setdefault(i, horizon)
    return sum(max(0.0, exited[i] - due[i]) for i in ids), exited


def pref_assignment(prob):
    """Each block -> most-preferred bay that can fit at least one orientation."""
    blocks = prob["blocks"]; bays = prob["bays"]
    assign = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        for j in order:
            W, H = bays[j]["width"], bays[j]["height"]
            ok = False
            for o in range(len(b["shape"])):
                mnx, mny, mxx, mxy = orient_bbox(b, o)
                if (mxx - mnx) <= W and (mxy - mny) <= H:
                    ok = True; break
            if ok:
                assign[i] = j; break
        assign.setdefault(i, order[0])
    return assign


def run(path, timelimit, use_baseline=False):
    prob = json.load(open(path, encoding="utf-8"))
    blocks = prob["blocks"]; m = len(prob["bays"])
    name = os.path.basename(path).replace(".json", "")
    if use_baseline:
        sol = baseline_greedy.greedyalgorithm(prob, timelimit)
        res = utils.check_feasibility(prob, sol)
        if not res["feasible"]:
            print(f"{name}: baseline INFEASIBLE, skip"); return
        place = {}; bexit = {}
        for t_str, ops in sol["operations"].items():
            for op in ops:
                if op["type"] == "ENTRY": place[op["block_id"]] = op["bay_id"]
                else: bexit[op["block_id"]] = int(t_str)
        baseT = {j: sum(max(0.0, bexit[i] - blocks[i]["due_date"])
                        for i in place if place[i] == j and i in bexit) for j in range(m)}
    else:
        place = pref_assignment(prob); baseT = {j: None for j in range(m)}
    print(f"\n{name} (assignment={'baseline' if use_baseline else 'preference'}):")
    tot_p = tot_entry = 0.0
    all_place = {}  # id -> (bay,x,y,o,entry,exit)
    for j in range(m):
        ids = [i for i in place if place[i] == j]
        if not ids: continue
        solve_bay.n_fallback = 0
        t0 = time.time()
        placed = solve_bay(prob, j, ids)
        Tp, exits = extract_tardiness(prob, j, placed)
        Tentry = sum(max(0.0, p["entry"] + blocks[p["id"]]["processing_time"] - blocks[p["id"]]["due_date"])
                     for p in placed)
        dt = time.time() - t0
        tot_p += Tp; tot_entry += Tentry
        for p in placed:
            all_place[p["id"]] = (j, p["x"], p["y"], p["o"], p["entry"], exits[p["id"]])
        bstr = f"T_base={baseT[j]:6.0f}  " if baseT[j] is not None else ""
        print(f"  bay{j}: n={len(ids):3d}  {bstr}T_proto={Tp:6.0f}  "
              f"(T_entry={Tentry:.0f} +extract={Tp-Tentry:.0f})  fallback={solve_bay.n_fallback}  {dt:.1f}s")
    # assemble + validate with the REAL oracle
    ops = {}
    for i, (j, x, y, o, en, ex) in all_place.items():
        ops.setdefault(str(int(ex)), []).append({"type": "EXIT", "block_id": i, "bay_id": j})
        ops.setdefault(str(int(en)), []).append(
            {"type": "ENTRY", "block_id": i, "bay_id": j, "x": int(x), "y": int(y), "orient_idx": o})
    # ensure EXIT-before-ENTRY ordering within each time key
    for k in ops:
        ops[k].sort(key=lambda d: 0 if d["type"] == "EXIT" else 1)
    # insert time keys in sorted order (check_feasibility reconstruction reads
    # them in insertion order; an EXIT key before its ENTRY key loses exit_time)
    ops = {k: ops[k] for k in sorted(ops, key=int)}
    res = utils.check_feasibility(prob, {"operations": ops})
    if res["feasible"]:
        print(f"  ORACLE: FEASIBLE  obj={res['objective']:.0f}  obj1(T)={res['obj1']:.0f}  "
              f"obj2={res['obj2']:.0f}  obj3={res['obj3']:.0f}")
    else:
        print(f"  ORACLE: INFEASIBLE stage={res['stage']}  {res['violations'][:2]}")
    print(f"  TOTAL: T_proto={tot_p:.0f}  (T_entry={tot_entry:.0f} +extract={tot_p-tot_entry:.0f})")


if __name__ == "__main__":
    tl = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    for p in sys.argv[2:]:
        run(p, tl)
