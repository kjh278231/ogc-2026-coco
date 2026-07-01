"""Cheapest falsification of the ORIENTATION lever (data-only, no packing).

find_slot tries orientations in index order and takes the first with a bottom-left slot, so
it effectively prefers orientation 0. If orientation 0 is already (near) the smallest-footprint
fitting orientation, there is no orientation headroom -- a min-area-first orientation policy
would change nothing. Measure, per block in its a_pref bay: orient-0 union area vs the MIN union
area over fitting orientations. ratio>1 => a tighter orientation is being ignored.

Run: ./.venv/Scripts/python.exe tools/_orient_data_probe.py
"""
from __future__ import annotations
import os, sys, json, math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("SOLVER_NUMBA", "1")
import packing as K            # noqa: E402
from solver import a_pref      # noqa: E402


def fits_bay(bd, bay, o):
    mnx, mny, mxx, mxy = K.orient_bbox(bd, o)
    return (math.ceil(max(0.0, -mnx)) + mxx <= bay["width"]
            and math.ceil(max(0.0, -mny)) + mxy <= bay["height"])


def main():
    INST = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    grand_ratios = []
    grand_improvable = 0
    grand_n = 0
    for name in INST:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        K.clear_packing_caches()
        blocks = prob["blocks"]; bays = prob["bays"]
        asg = a_pref(prob)
        ratios = []
        improvable = 0
        n_multi = 0   # blocks with >1 fitting orientation (where the lever can act)
        for i, bd in enumerate(blocks):
            bay = bays[asg[i]]
            fitting = [o for o in range(len(bd["shape"])) if fits_bay(bd, bay, o)]
            if not fitting:
                fitting = list(range(len(bd["shape"])))
            if len(fitting) > 1:
                n_multi += 1
            areas = {o: K._local_footprint(bd, o).area if K._local_footprint(bd, o) else 0.0
                     for o in fitting}
            # orientation the packer would use = lowest fitting index (it prefers index order)
            o0 = min(fitting)
            a0 = areas[o0]
            amin = min(areas.values())
            if amin > 1e-9:
                r = a0 / amin
                ratios.append(r)
                if r > 1.05:
                    improvable += 1
        mr = sum(ratios) / len(ratios) if ratios else 1.0
        grand_ratios += ratios
        grand_improvable += improvable
        grand_n += len(ratios)
        print(f"{name}: n={len(blocks)} multi-orient={n_multi} "
              f"mean(orient0/min-area)={mr:.3f}  blocks with tighter orient ignored (>5%): "
              f"{improvable} ({100*improvable/len(blocks):.0f}%)")
    gmr = sum(grand_ratios) / len(grand_ratios)
    print(f"\nGRAND mean orient0/min-area ratio = {gmr:.3f}  "
          f"| blocks ignoring a >5%-tighter orient = {grand_improvable}/{grand_n} "
          f"({100*grand_improvable/grand_n:.0f}%)")
    print("Read: ratio~1.0 and low %% => orientation-0 is already tight => orientation lever DEAD.")


if __name__ == "__main__":
    main()
