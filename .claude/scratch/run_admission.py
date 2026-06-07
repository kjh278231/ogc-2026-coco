"""Exp B: was the admission delay of each tardy block space-forced or myopic?

For each tardy block i (baseline exit_i > due_i), scan its on-time entry window
[release_i, due_i - proc_i].  At each integer time t in that window, take the set
of blocks baseline actually had present in i's bay at t, and search positions x
orientations for a slot where i could be placed collision-free AND crane-ENTRY
feasible (utils.check_entry).  If ANY (t, x, y, orient) works, on-time admission
was geometrically possible under baseline's own occupancy -> the delay was
MYOPIC (recoverable headroom).  If none works across the whole window -> the
slot was genuinely congested (under baseline's occupancy).

A 'YES' is strong evidence of headroom; a 'NO' is weaker (baseline's occupancy
itself may be bad), so a high YES-rate is the decisive positive signal.
"""
import sys, os, json
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline"))
import baseline_greedy, utils
from utils import Bay, Block, check_entry


def orient_bbox(block_data, o):
    xs = []; ys = []
    for layer in block_data["shape"][o]["layers"]:
        for x, y in layer:
            xs.append(x); ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def slot_exists(bay, present_objs, block_data, bid, W, H, step):
    """Try to find any (x,y,orient) where block bid fits + crane-entry OK."""
    for o in range(len(block_data["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(block_data, o)
        # reference point is first vertex of layer0 = (0,0); placing ref at (x,y)
        # shifts all verts by (x,y). To stay in bay: x+mnx>=0 .. x+mxx<=W etc.
        x_lo = int(-mnx); x_hi = int(W - mxx)
        y_lo = int(-mny); y_hi = int(H - mxy)
        if x_hi < x_lo or y_hi < y_lo:
            continue
        for x in range(x_lo, x_hi + 1, step):
            for y in range(y_lo, y_hi + 1, step):
                cand = Block(block_id=bid, block_data=block_data, x=x, y=y, orient_idx=o)
                if not check_entry(bay, present_objs, cand):
                    return (x, y, o)
    return None


def run(path, timelimit, step=2, maxtardy=40):
    prob = json.load(open(path, encoding="utf-8"))
    blocks = prob["blocks"]; bays = prob["bays"]
    sol = baseline_greedy.greedyalgorithm(prob, timelimit)
    res = utils.check_feasibility(prob, sol)
    name = os.path.basename(path).replace(".json", "")
    if not res["feasible"]:
        print(f"{name}: INFEASIBLE, skip"); return
    place = {}; exits = {}
    for t_str, ops in sol["operations"].items():
        t = int(t_str)
        for op in ops:
            i = op["block_id"]
            if op["type"] == "ENTRY":
                place[i] = (op.get("x", 0), op.get("y", 0), op.get("orient_idx", 0), t, op["bay_id"])
            else:
                exits[i] = t
    tardy = [i for i in range(len(blocks))
             if i in exits and exits[i] > blocks[i]["due_date"]]
    # cap work
    tardy_sorted = sorted(tardy, key=lambda i: exits[i] - blocks[i]["due_date"], reverse=True)
    sample = tardy_sorted[:maxtardy]
    myopic = 0; congested = 0
    for i in sample:
        bd = blocks[i]; j = place[i][4]
        bay = Bay.from_dict(bays[j], j); W = bays[j]["width"]; H = bays[j]["height"]
        R = bd["release_time"]; P = bd["processing_time"]; D = bd["due_date"]
        t_last = D - P  # latest on-time entry
        found = None
        for t in range(R, t_last + 1):
            present_ids = [k for k in place
                           if k != i and place[k][4] == j
                           and place[k][3] <= t < exits.get(k, 10**9)]
            present_objs = [Block(block_id=k, block_data=blocks[k],
                                  x=int(round(place[k][0])), y=int(round(place[k][1])),
                                  orient_idx=place[k][2]) for k in present_ids]
            found = slot_exists(bay, present_objs, bd, i, W, H, step)
            if found:
                break
        if found:
            myopic += 1
        else:
            congested += 1
    n = len(sample)
    print(f"{name}: tardy={len(tardy)} sampled={n}  "
          f"on-time-slot-existed(MYOPIC)={myopic}  congested={congested}  "
          f"myopic_frac={myopic/n:.2f}" if n else f"{name}: no tardy blocks")


if __name__ == "__main__":
    tl = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    paths = sys.argv[2:]
    for p in paths:
        run(p, tl)
