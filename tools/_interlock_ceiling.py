"""Optimistic CEILING for the interlocking lever (STOW frontier), no new packer.

Physical model: every block rests on the floor (layer 0 at z=0). Two blocks may share an
(x,y) column only via OVERHANG: block B's upper layer k>0 can hang OVER block A's floor
(layer 0) iff A has no layer at that height there (j<k never obstructs the crane). The
current packer forbids ALL union-footprint overlap, so it wastes every overhang notch.

Optimistic capacity ceiling of interlocking = pack to the LAYER-0 (floor) footprint area
instead of the UNION footprint area. This IGNORES the crane-sweep constraint, so it is an
upper bound: if even this optimistic relaxation barely helps the saturated bays, interlocking
is not worth its (feasibility-critical) cost. Compares demand_peak_util computed with union
area (what the current packer effectively needs) vs layer-0 area (the interlocking floor).

Run AFTER the wall A/B finishes (clean timing):
  ./.venv/Scripts/python.exe tools/_interlock_ceiling.py
"""
from __future__ import annotations
import os, sys, json, math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("SOLVER_NUMBA", "1")
import packing as K          # noqa: E402
from solver import a_pref    # noqa: E402
from shapely.geometry import Polygon  # noqa: E402


def union_area(bd, o):
    fp = K._local_footprint(bd, o)
    return fp.area if fp is not None else 0.0


def layer0_area(bd, o):
    """Area of just the floor layer (layer 0) of orientation o."""
    l0 = bd["shape"][o]["layers"][0]
    if len(l0) < 3:
        return 0.0
    try:
        p = Polygon(l0)
        if not p.is_valid:
            p = p.buffer(0)
        return p.area
    except Exception:
        return 0.0


def fitting_orients(bd, bay):
    out = []
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = K.orient_bbox(bd, o)
        if (math.ceil(max(0.0, -mnx)) + mxx <= bay["width"]
                and math.ceil(max(0.0, -mny)) + mxy <= bay["height"]):
            out.append(o)
    return out or list(range(len(bd["shape"])))


def peak_util(prob, j, ids, area_fn):
    bay = prob["bays"][j]; bay_area = bay["width"] * bay["height"]
    blocks = prob["blocks"]; evts = []
    for i in ids:
        bd = blocks[i]; os_ = fitting_orients(bd, bay)
        a = min(area_fn(bd, o) for o in os_)
        r = bd["release_time"]; p = bd["processing_time"]
        evts.append((r, +a)); evts.append((r + p, -a))
    evts.sort(key=lambda e: (e[0], -e[1]))
    cur = peak = 0.0
    for _, da in evts:
        cur += da; peak = max(peak, cur)
    return peak / bay_area


def main():
    INST = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    ratios_all = []
    for name in INST:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        K.clear_packing_caches()
        blocks = prob["blocks"]; m = len(prob["bays"])
        # per-block union/layer0 ratio (orient 0) -> optimistic capacity multiplier
        rs = []
        for bd in blocks:
            u = union_area(bd, 0); l0 = layer0_area(bd, 0)
            if l0 > 1e-9:
                rs.append(u / l0)
        ratios_all += rs
        meanr = sum(rs) / len(rs) if rs else 1.0
        asg = a_pref(prob)
        bay_ids = {j: [i for i in asg if asg[i] == j] for j in range(m)}
        print(f"\n==== {name}: mean union/layer0 ratio={meanr:.2f} (>1 => overhang to exploit)")
        for j in range(m):
            ids = bay_ids[j]
            if not ids:
                continue
            uu = peak_util(prob, j, ids, union_area)
            ll = peak_util(prob, j, ids, layer0_area)
            mark = ""
            if uu > 1.0 and ll < 1.0:
                mark = "  <== interlocking could DE-SATURATE"
            elif uu > 1.0:
                mark = "  (still saturated even on floor area)"
            print(f"     bay{j}: nblk={len(ids):3d} union_util={uu:5.2f} floor_util={ll:5.2f}"
                  f"  reduction={100*(uu-ll)/uu if uu>0 else 0:4.0f}%{mark}")
    mr = sum(ratios_all) / len(ratios_all)
    print(f"\nGRAND mean union/layer0 ratio = {mr:.3f}  "
          f"(=> optimistic interlocking capacity multiplier; 1.0 = no headroom)")


if __name__ == "__main__":
    main()
