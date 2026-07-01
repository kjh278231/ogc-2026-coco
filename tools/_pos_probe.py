"""Cheap falsification of the POSITION lever. find_slot uses bottom-left first-fit (scan y
outer asc, x inner asc -> lowest, then leftmost). Does the scan DIRECTION leave exploitable
fragmentation? Replicate the AABB packer (EDD order, to isolate position from order) with three
scan directions and take the per-bay oracle. BL~=oracle => first-fit position is settled;
oracle << BL => a smarter position rule (best-fit) is a lever worth building.

Run: ./.venv/Scripts/python.exe tools/_pos_probe.py
"""
from __future__ import annotations
import os, sys, json, math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import packing as K            # noqa: E402
from solver import a_pref      # noqa: E402
from utils import Bay, Block, check_entry  # noqa: E402


def find_slot_dir(bay, present, ov_boxes, bd, bid, W, H, step, direction):
    """AABB earliest feasible (x,y,o) under a given scan DIRECTION. Orientation index order
    (same as find_slot). direction: 'BL' y-asc/x-asc, 'LB' x-asc/y-asc, 'TL' y-desc/x-asc."""
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = K.orient_bbox(bd, o)
        x_start = math.ceil(max(0.0, -mnx)); x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny)); y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        lbx0, lby0, lbx1, lby1 = K._local_box(bd, o)
        ys = range(y_start, y_end + 1, step)
        xs = range(x_start, x_end + 1, step)
        if direction == "TL":
            ys = range(y_end, y_start - 1, -step)
        if direction == "LB":
            outer, inner, oaxis = xs, ys, "x"
        else:
            outer, inner, oaxis = ys, xs, "y"
        for a in outer:
            for b in inner:
                x, y = (a, b) if oaxis == "x" else (b, a)
                cx0 = lbx0 + x; cx1 = lbx1 + x; cy0 = lby0 + y; cy1 = lby1 + y
                if any(cx0 < bb[2] and bb[0] < cx1 and cy0 < bb[3] and bb[1] < cy1 for bb in ov_boxes):
                    continue
                cand = Block(bid, bd, x, y, o)
                if check_entry(bay, present, cand, fast=True):
                    continue
                return (x, y, o)
    return None


def solve_bay_dir(prob, j, ids, direction, step=2, tcap=200):
    bays = prob["bays"]; blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j); W = bays[j]["width"]; H = bays[j]["height"]
    placed = []
    order = sorted(ids, key=lambda i: (blocks[i]["due_date"], blocks[i]["processing_time"]))
    for i in order:
        bd = blocks[i]; R = bd["release_time"]; P = bd["processing_time"]; chosen = None
        for t in range(R, R + tcap):
            present = [p["blk"] for p in placed if p["entry"] <= t < p["exit"]]
            ov = [p for p in placed if p["entry"] < t + P and t < p["exit"]]
            slot = find_slot_dir(bay, present, [p["bb"] for p in ov], bd, i, W, H, step, direction)
            if slot:
                chosen = (t, slot[0], slot[1], slot[2]); break
        if chosen is None:
            t = max((p["exit"] for p in placed), default=R)
            for tt in range(t, t + 1000):
                present = [p["blk"] for p in placed if p["entry"] <= tt < p["exit"]]
                ov = [p for p in placed if p["entry"] < tt + P and tt < p["exit"]]
                slot = find_slot_dir(bay, present, [p["bb"] for p in ov], bd, i, W, H, step, direction)
                if slot:
                    chosen = (tt, slot[0], slot[1], slot[2]); break
            if chosen is None:
                chosen = (t, 0, 0, 0)
        blk = Block(i, bd, chosen[1], chosen[2], chosen[3])
        placed.append({"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
                       "entry": chosen[0], "exit": chosen[0] + P, "blk": blk, "bb": blk.bounding_rect()})
    return placed


def z1(prob, j, ids, direction):
    return K.extract_tardiness(prob, j, solve_bay_dir(prob, j, ids, direction))[0]


def main():
    INST = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    tot = {"BL": 0.0, "LB": 0.0, "TL": 0.0, "oracle": 0.0}
    for name in INST:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        K.clear_packing_caches()
        m = len(prob["bays"]); asg = a_pref(prob)
        for j in range(m):
            ids = [i for i in asg if asg[i] == j]
            if not ids:
                continue
            zb = z1(prob, j, ids, "BL")
            if zb == 0:
                continue  # only contended bays matter
            zl = z1(prob, j, ids, "LB"); zt = z1(prob, j, ids, "TL")
            orc = min(zb, zl, zt)
            tot["BL"] += zb; tot["LB"] += zl; tot["TL"] += zt; tot["oracle"] += orc
            print(f"{name} bay{j}: BL={zb:.0f} LB={zl:.0f} TL={zt:.0f} oracle={orc:.0f}")
    print(f"\nTOTAL(AABB,EDD, tardy bays): BL={tot['BL']:.0f} LB={tot['LB']:.0f} "
          f"TL={tot['TL']:.0f} ORACLE={tot['oracle']:.0f}  "
          f"(oracle vs BL: {100*(tot['oracle']-tot['BL'])/tot['BL'] if tot['BL'] else 0:+.0f}%)")
    print("Read: oracle ~= BL => position first-fit settled (dead lever); oracle << BL => build best-fit.")


if __name__ == "__main__":
    main()
