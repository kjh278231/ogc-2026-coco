"""Does ORDER have headroom BEYOND the 4 fixed orders? Order was the dominant placement lever
(-24% bay-oracle, captured by solve_bay_best's 4-order best-of). This probes whether a per-bay
order LOCAL SEARCH (swap pairs in the placement order, re-pack, keep improvements) beats the
4-order best-of -- i.e. is there a further 'new lever' in order optimization for P2-P5.

Start from the best of the 4 orders (mask packer), then hill-climb by swapping order positions.
Reports Z1: best-of-4 vs +local-search on the hard instances' tardy bays.

Run: ./.venv/Scripts/python.exe tools/_order_ls_probe.py
"""
from __future__ import annotations
import os, sys, json, random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("SOLVER_NUMBA", "1")
import packing as K            # noqa: E402
from solver import a_pref      # noqa: E402


def z1_of_order(prob, j, order):
    placed = K.solve_bay(prob, j, list(order), mask=True, mask_R=8, order=list(order))
    return K.extract_tardiness(prob, j, placed)[0]


def main():
    INST = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    SWAPS = 300
    tot_best4 = tot_ls = 0.0
    for name in INST:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        K.clear_packing_caches()
        b = prob["blocks"]; m = len(prob["bays"]); asg = a_pref(prob)
        for j in range(m):
            ids = [i for i in asg if asg[i] == j]
            if not ids:
                continue
            cands = K._order_candidates(prob, ids)         # the deployed 4-order set
            zs = [(z1_of_order(prob, j, o), o) for o in cands]
            zbest, obest = min(zs, key=lambda t: t[0])
            if zbest == 0:
                continue                                    # only contended bays
            # order hill-climb from the best-of-4 order: swap two positions, re-pack, keep if better
            rng = random.Random(7 + j)
            cur = list(obest); cur_z = zbest
            n = len(cur)
            for _ in range(SWAPS):
                if cur_z == 0:
                    break
                a, c = rng.randrange(n), rng.randrange(n)
                if a == c:
                    continue
                trial = list(cur); trial[a], trial[c] = trial[c], trial[a]
                tz = z1_of_order(prob, j, trial)
                if tz < cur_z - 1e-9:
                    cur, cur_z = trial, tz
            tot_best4 += zbest; tot_ls += cur_z
            tag = "  <== LS win" if cur_z < zbest - 1e-9 else ""
            print(f"{name} bay{j}: best-of-4={zbest:.0f}  +order-LS={cur_z:.0f}"
                  f"  ({100*(cur_z-zbest)/zbest:+.0f}%){tag}")
    print(f"\nTOTAL (tardy bays, mask): best-of-4={tot_best4:.0f}  +order-LS={tot_ls:.0f}  "
          f"(LS vs best-of-4: {100*(tot_ls-tot_best4)/tot_best4 if tot_best4 else 0:+.1f}%)")
    print("Read: LS ~= best-of-4 => order fully captured; LS << best-of-4 => order-LS is a new lever.")


if __name__ == "__main__":
    main()
