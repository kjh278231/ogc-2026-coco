"""Cheap falsification of the ORDER lever in solve_bay.

solve_bay places blocks in a FIXED order (due_date, processing_time) = EDD. Since
temporal_floor=0, all Z1 is contention: the order decides who gets the good early slots.
This probe replicates solve_bay's mask path EXACTLY but with an injectable order, and
measures Z1 under EDD vs alternative orders + an oracle min over random orders, on the
tardy bays of the hard instances (a_pref assignment). Big oracle gain => order search is a
real placement lever; flat => order is not where the headroom is.

Run:  ./.venv/Scripts/python.exe tools/_order_probe.py
"""
from __future__ import annotations
import os, sys, json, math, random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("SOLVER_NUMBA", "1")

import packing as K          # noqa: E402
from solver import a_pref    # noqa: E402
from utils import Bay, Block # noqa: E402


def solve_bay_order(prob, j, order, step=2, tcap=200, mask_R=8):
    """Faithful copy of K.solve_bay's mask=True path, but with explicit placement `order`
    (a permutation of ids) instead of the internal EDD sort. Everything else identical."""
    bays = prob["bays"]; blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j)
    W = bays[j]["width"]; H = bays[j]["height"]
    placed = []
    for i in order:
        bd = blocks[i]; R = bd["release_time"]; P = bd["processing_time"]
        chosen = None
        for t in range(R, R + tcap):
            present = [p["blk"] for p in placed if p["entry"] <= t < p["exit"]]
            overlap = [p for p in placed if p["entry"] < t + P and t < p["exit"]]
            ov_objs = [p["blk"] for p in overlap]
            ov_boxes = [p["bb"] for p in overlap]
            slot = K.find_slot(bay, present, ov_objs, bd, i, W, H, step, ov_boxes)
            if slot is None:
                ov_bm = [(p["bb"], p["mask"], p["mix0"], p["miy0"]) for p in placed
                         if p["entry"] < t + P and t < p["exit"]]
                slot = K.find_slot_mask(bay, present, ov_bm, bd, i, W, H, step, mask_R)
            if slot:
                chosen = (t, slot[0], slot[1], slot[2]); break
        if chosen is None:
            t = max((p["exit"] for p in placed), default=R)
            for tt in range(t, t + 1000):
                present = [p["blk"] for p in placed if p["entry"] <= tt < p["exit"]]
                overlap = [p for p in placed if p["entry"] < tt + P and tt < p["exit"]]
                ov_objs = [p["blk"] for p in overlap]; ov_boxes = [p["bb"] for p in overlap]
                slot = K.find_slot(bay, present, ov_objs, bd, i, W, H, step, ov_boxes)
                if slot:
                    chosen = (tt, slot[0], slot[1], slot[2]); break
            if chosen is None:
                o_fit = next((o for o in range(len(bd["shape"]))
                              if math.ceil(max(0.0, -K.orient_bbox(bd, o)[0])) + K.orient_bbox(bd, o)[2] <= W
                              and math.ceil(max(0.0, -K.orient_bbox(bd, o)[1])) + K.orient_bbox(bd, o)[3] <= H), 0)
                mnx, mny, _, _ = K.orient_bbox(bd, o_fit)
                chosen = (t, math.ceil(max(0.0, -mnx)), math.ceil(max(0.0, -mny)), o_fit)
        blk_o = Block(i, bd, chosen[1], chosen[2], chosen[3])
        rec = {"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
               "entry": chosen[0], "exit": chosen[0] + P, "blk": blk_o, "bb": blk_o.bounding_rect()}
        lm = K._local_mask(bd, chosen[3], mask_R)
        rec["mask"] = lm; rec["mix0"] = lm.ix0 + chosen[1] * mask_R; rec["miy0"] = lm.iy0 + chosen[2] * mask_R
        placed.append(rec)
    return placed


def z1(prob, j, order):
    placed = solve_bay_order(prob, j, order)
    T, _ = K.extract_tardiness(prob, j, placed)
    return T


def orders_for(prob, ids):
    b = prob["blocks"]
    return {
        "edd": sorted(ids, key=lambda i: (b[i]["due_date"], b[i]["processing_time"])),
        "leastslack": sorted(ids, key=lambda i: (b[i]["due_date"] - b[i]["processing_time"], b[i]["due_date"])),
        "edd_areadesc": sorted(ids, key=lambda i: (b[i]["due_date"], -K.orient_bbox(b[i], 0)[2] * K.orient_bbox(b[i], 0)[3])),
        "release": sorted(ids, key=lambda i: (b[i]["release_time"], b[i]["due_date"])),
    }


def main():
    HARD = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    K_RAND = 6
    grand = {}
    for name in HARD:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        K.clear_packing_caches()
        m = len(prob["bays"]); asg = a_pref(prob)
        bay_ids = {j: [i for i in asg if asg[i] == j] for j in range(m)}
        print(f"\n==== {name} ====")
        tot = {"edd": 0.0, "leastslack": 0.0, "edd_areadesc": 0.0, "release": 0.0, "rand_min": 0.0, "oracle": 0.0}
        for j in range(m):
            ids = bay_ids[j]
            if not ids:
                continue
            named = orders_for(prob, ids)
            zs = {k: z1(prob, j, o) for k, o in named.items()}
            if zs["edd"] == 0 and all(v == 0 for v in zs.values()):
                for k in ("edd", "leastslack", "edd_areadesc", "release"):
                    tot[k] += 0
                tot["rand_min"] += 0; tot["oracle"] += 0
                continue
            rng = random.Random(12345 + j)
            rmin = min(z1(prob, j, rng.sample(ids, len(ids))) for _ in range(K_RAND))
            oracle = min(min(zs.values()), rmin)
            for k in ("edd", "leastslack", "edd_areadesc", "release"):
                tot[k] += zs[k]
            tot["rand_min"] += rmin; tot["oracle"] += oracle
            if zs["edd"] > 0 or oracle > 0:
                print(f"  bay{j}: edd={zs['edd']:7.0f} leastslack={zs['leastslack']:7.0f} "
                      f"areadesc={zs['edd_areadesc']:7.0f} release={zs['release']:7.0f} "
                      f"rand_min={rmin:7.0f}  ORACLE={oracle:7.0f}")
        ge = tot["edd"]
        print(f"  TOTAL: edd={ge:.0f} leastslack={tot['leastslack']:.0f} areadesc={tot['edd_areadesc']:.0f} "
              f"release={tot['release']:.0f} rand_min={tot['rand_min']:.0f} ORACLE={tot['oracle']:.0f}"
              f"  (oracle vs edd: {100*(tot['oracle']-ge)/ge if ge>0 else 0:+.0f}%)")
        grand[name] = tot
    print("\n==== GRAND (Z1 sums) ====")
    se = sum(t["edd"] for t in grand.values()); so = sum(t["oracle"] for t in grand.values())
    sr = sum(t["rand_min"] for t in grand.values())
    print(f"  edd={se:.0f}  rand_min={sr:.0f}  oracle={so:.0f}   oracle vs edd: {100*(so-se)/se if se>0 else 0:+.1f}%")


if __name__ == "__main__":
    main()
