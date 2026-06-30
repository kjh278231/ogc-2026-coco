"""Cheap falsification: where does Z1 come from on the hard-packing instances?

No new packer. Uses (a) pure data (demand-peak utilisation if every block entered
at its release) to separate genuine area over-subscription from packer inefficiency,
and (b) the existing solve_bay at AABB vs mask geometry as an instrument to measure how
much geometry headroom the current escalation already captures.

Run:  ./.venv/Scripts/python.exe tools/_pack_diag.py
"""
from __future__ import annotations
import os, sys, json, math, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
sys.path.insert(0, str(BRIDGE))
os.environ.setdefault("SOLVER_NUMBA", "1")   # bit-identical, faster mask/AABB scan

import packing as K           # noqa: E402
from solver import a_pref     # noqa: E402  (assignment heuristic only)

HARD = ["T20", "T14", "T13", "T11", "T18"]
EASY = ["T9", "T2", "T6"]
INSTANCES = HARD + EASY


def union_area(bd, o):
    fp = K._local_footprint(bd, o)
    return fp.area if fp is not None else 0.0


def aabb_area(bd, o):
    mnx, mny, mxx, mxy = K.orient_bbox(bd, o)
    return (mxx - mnx) * (mxy - mny)


def fitting_orients(bd, bay):
    out = []
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = K.orient_bbox(bd, o)
        if (math.ceil(max(0.0, -mnx)) + mxx <= bay["width"]
                and math.ceil(max(0.0, -mny)) + mxy <= bay["height"]):
            out.append(o)
    return out or list(range(len(bd["shape"])))


def demand_peak(prob, j, ids):
    """If every block in bay j entered at its release (exit=release+proc), what is the
    peak concurrent union-footprint area / bay area, and peak concurrent count?
    Optimistic: per block use the min-union-area fitting orientation (best case to fit)."""
    bay = prob["bays"][j]
    bay_area = bay["width"] * bay["height"]
    blocks = prob["blocks"]
    evts = []  # (time, +area/-area)
    for i in ids:
        bd = blocks[i]
        os_ = fitting_orients(bd, bay)
        a = min(union_area(bd, o) for o in os_)
        r = bd["release_time"]; p = bd["processing_time"]
        evts.append((r, +a, +1))
        evts.append((r + p, -a, -1))
    # sweep
    evts.sort(key=lambda e: (e[0], -e[1]))  # at a tie, additions first (conservative peak)
    cur_a = 0.0; cur_n = 0; peak_a = 0.0; peak_n = 0
    for _, da, dn in evts:
        cur_a += da; cur_n += dn
        peak_a = max(peak_a, cur_a); peak_n = max(peak_n, cur_n)
    return peak_a / bay_area, peak_n, bay_area


def z1_bay(prob, j, ids, mode):
    if mode == "aabb":
        placed = K.solve_bay(prob, j, ids, poly=False)
    elif mode == "mask":
        placed = K.solve_bay(prob, j, ids, mask=True, mask_R=8)
    else:
        raise ValueError(mode)
    T, _ = K.extract_tardiness(prob, j, placed)
    return T


def layer_profile(prob):
    blocks = prob["blocks"]
    nlayers = []
    stepped = 0   # blocks whose union footprint area > layer-0 area (upper layers extend out)
    multilayer = 0
    box_ratio = []  # union-area / aabb-area at orient 0 (1.0 = perfectly boxy)
    for bd in blocks:
        sh0 = bd["shape"][0]
        nl = len(sh0["layers"])
        nlayers.append(nl)
        if nl > 1:
            multilayer += 1
        u = union_area(bd, 0)
        ab = aabb_area(bd, 0)
        if ab > 0:
            box_ratio.append(u / ab)
        # layer-0 area vs union: if layers differ, union > layer0 (interlock potential)
        l0 = sh0["layers"][0]
        if len(l0) >= 3:
            from shapely.geometry import Polygon
            try:
                a0 = Polygon(l0).buffer(0).area
                if u > a0 + 1e-6:
                    stepped += 1
            except Exception:
                pass
    n = len(blocks)
    from collections import Counter
    return {
        "n": n,
        "nlayer_hist": dict(sorted(Counter(nlayers).items())),
        "pct_multilayer": 100.0 * multilayer / n,
        "pct_stepped": 100.0 * stepped / n,
        "mean_box_ratio": sum(box_ratio) / len(box_ratio) if box_ratio else 0.0,
    }


def main():
    for name in INSTANCES:
        path = ROOT / "train" / f"{name}.json"
        prob = json.load(open(path))
        K.clear_packing_caches()
        blocks = prob["blocks"]; bays = prob["bays"]
        m = len(bays); n = len(blocks)
        w = prob["weights"]
        # temporal floor: Sigma max(0, R+P-D) (achievable iff every block enters at release)
        tfloor = sum(max(0, b["release_time"] + b["processing_time"] - b["due_date"]) for b in blocks)
        lp = layer_profile(prob)
        asg = a_pref(prob)
        bay_ids = {j: [i for i in asg if asg[i] == j] for j in range(m)}
        print(f"\n==== {name}: n={n} m={m} w1={w['w1']} w2={w['w2']} w3={w['w3']} "
              f"temporal_floor(Z1)={tfloor}")
        print(f"     layers: hist={lp['nlayer_hist']} multilayer={lp['pct_multilayer']:.0f}% "
              f"stepped={lp['pct_stepped']:.0f}% mean_box_ratio={lp['mean_box_ratio']:.2f}")
        tot_aabb = tot_mask = 0.0
        for j in range(m):
            ids = bay_ids[j]
            if not ids:
                continue
            dutil, dcount, barea = demand_peak(prob, j, ids)
            t0 = time.time()
            za = z1_bay(prob, j, ids, "aabb")
            zm = z1_bay(prob, j, ids, "mask")
            tot_aabb += za; tot_mask += zm
            flag = ""
            if zm > 0:
                flag = "  <-- TARDY"
            print(f"     bay{j}: nblk={len(ids):3d} demand_peak_util={dutil:5.2f} "
                  f"peak_concurrent={dcount:3d}  Z1_aabb={za:8.0f} Z1_mask={zm:8.0f}"
                  f"  (geom_gain={100*(za-zm)/za if za>0 else 0:4.0f}%){flag}")
        print(f"     TOTAL Z1: aabb={tot_aabb:.0f} mask={tot_mask:.0f} "
              f"geom_gain={100*(tot_aabb-tot_mask)/tot_aabb if tot_aabb>0 else 0:.0f}%  "
              f"(w1*Z1_mask={w['w1']*tot_mask:.0f})")


if __name__ == "__main__":
    main()
