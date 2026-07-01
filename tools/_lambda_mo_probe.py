"""Cheap falsification of the Z3 lever: does multi-order shift the best MIP-anchor lambda?

The MIP anchor solves argmin(lam*w2*Z2 + w3*Z3). Lower lam => lower Z3 (more preference) but
more crowding => higher Z1. The shipped PRISM uses lam=16 (chosen when the packer was single-EDD-
order). multi-order now packs crowded bays with far less tardiness, so a LOWER-lam (lower-Z3)
anchor that was previously un-repairable may now pack near Z1=0 -> lower final objective.

For each lam, compute the anchor, pack every bay with the multi-order best-of packer, and report
Z1, Z3, and the dominant floor w1*Z1 + w3*Z3. If a lam < 16 wins the floor under multi-order,
the Z3 lever is 're-open the lambda spectrum with multi-order'.

Run: ./.venv/Scripts/python.exe tools/_lambda_mo_probe.py
"""
from __future__ import annotations
import os, sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prism"))
sys.path.append(str(ROOT / "bridge"))
os.environ.setdefault("SOLVER_NUMBA", "1")
import prism_engine as P     # noqa: E402
import packing as K          # noqa: E402
import solver as S           # noqa: E402


def floor_for_anchor(prob, A):
    m = len(prob["bays"]); w = prob["weights"]
    z1 = 0.0
    for j in range(m):
        ids = [i for i in A if A[i] == j]
        if not ids:
            continue
        _, T, _ = K.solve_bay_best(prob, j, ids, mask=True, mask_R=8)
        z1 += T
    z2, z3 = S.obj23(prob, A)
    return z1, z2, z3, w["w1"] * z1 + w["w3"] * z3


def main():
    INST = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    LAMS = [1.0, 4.0, 8.0, 16.0, 64.0]
    for name in INST:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        K.clear_packing_caches()
        w = prob["weights"]
        rows = []
        for lam in LAMS:
            A = P.mip_anchor(prob, lam, 4.0)
            if A is None:
                rows.append((lam, None)); continue
            z1, z2, z3, fl = floor_for_anchor(prob, A)
            rows.append((lam, (z1, z2, z3, fl)))
        # also the a_pref heuristic anchor for reference
        Ap = S.a_pref(prob)
        z1p, z2p, z3p, flp = floor_for_anchor(prob, Ap)
        best = min((r for r in rows if r[1]), key=lambda r: r[1][3])
        print(f"\n==== {name} (w1={w['w1']} w3={w['w3']}) ====")
        print(f"   a_pref       : Z1={z1p:6.0f} Z3={z3p:6.0f} floor(w1Z1+w3Z3)={flp:12.0f}")
        for lam, r in rows:
            if r is None:
                print(f"   lam={lam:5.0f}    : (no anchor)"); continue
            z1, z2, z3, fl = r
            mark = "  <== BEST" if (lam, r) == best else ("  [shipped]" if lam == 16 else "")
            print(f"   lam={lam:5.0f}    : Z1={z1:6.0f} Z3={z3:6.0f} floor={fl:12.0f}{mark}")
    print("\nRead: if BEST lam < 16 (with multi-order pack), lower-Z3 anchors are now unlocked "
          "=> re-open the lambda spectrum. If lam=16 stays best, the Z3 floor is anchor-settled.")


if __name__ == "__main__":
    main()
