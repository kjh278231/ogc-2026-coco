"""Cheap falsification of the SWAP-move Z3 lever. The assignment search only RELOCATES (move
one block to another bay). A SWAP (exchange blocks i<->k between their bays) reaches states
relocation cannot in one step -- e.g. two blocks that each prefer the other's bay, where moving
either alone raises Z1/Z2. Test: converge a RELOCATION hill-climb to a local optimum (multi-order
eval), then run a SWAP hill-climb from there. If swaps improve the relocation-optimum, the swap
move is a real Z3 lever; if not, relocation already captures it.

Run: SOLVER_MULTIORDER on. ./.venv/Scripts/python.exe tools/_swap_probe.py
"""
from __future__ import annotations
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("SOLVER_MASK", "1")
os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
os.environ.setdefault("SOLVER_NUMBA", "1")
os.environ.setdefault("SOLVER_MULTIORDER", "1")   # match the deployed packer
import solver as S           # noqa: E402
from packing import fits     # noqa: E402


def relocation_climb(prob, assign, cache, deadline):
    """First-improving relocation hill-climb to a local optimum (like S._climb but simplest)."""
    cur = dict(assign); cur_tot, _ = S.total_obj(prob, cur, cache)
    bays = prob["bays"]; blocks = prob["blocks"]; m = len(bays)
    improved = True
    while improved and time.time() < deadline:
        improved = False
        for i in list(cur):
            for j in range(m):
                if j == cur[i] or not fits(blocks[i], bays[j]):
                    continue
                trial = dict(cur); trial[i] = j
                t, _ = S.total_obj(prob, trial, cache)
                if t < cur_tot - 1e-9:
                    cur, cur_tot = trial, t; improved = True; break
            if improved:
                break
    return cur, cur_tot


def swap_climb(prob, assign, cache, deadline):
    """First-improving SWAP hill-climb from `assign` (exchange two blocks' bays)."""
    cur = dict(assign); cur_tot, _ = S.total_obj(prob, cur, cache)
    ids = list(cur); improved = True; nswap = 0
    while improved and time.time() < deadline:
        improved = False
        for a in range(len(ids)):
            i = ids[a]
            for b in range(a + 1, len(ids)):
                k = ids[b]
                if cur[i] == cur[k]:
                    continue
                if not (fits(prob["blocks"][i], prob["bays"][cur[k]])
                        and fits(prob["blocks"][k], prob["bays"][cur[i]])):
                    continue
                trial = dict(cur); trial[i], trial[k] = cur[k], cur[i]
                t, _ = S.total_obj(prob, trial, cache)
                if t < cur_tot - 1e-9:
                    cur, cur_tot = trial, t; improved = True; nswap += 1; break
            if improved:
                break
    return cur, cur_tot, nswap


def main():
    INST = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    for name in INST:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        S._POOL.clear(); S.clear_packing_caches(); S._EVALS = 0; S._EVAL_LIMIT = None
        cache = {}
        # best heuristic seed
        base = min((fn(prob) for fn in (S.a_pref, S.a_balanced_load, S.a_pref_capped)),
                   key=lambda a: S.total_obj(prob, a, cache)[0])
        r_asg, r_tot = relocation_climb(prob, base, cache, time.time() + 40)
        s_asg, s_tot, nswap = swap_climb(prob, r_asg, cache, time.time() + 40)
        # then alternate once more (relocation after swaps can open new relocations)
        r2, r2_tot = relocation_climb(prob, s_asg, cache, time.time() + 20)
        s2, s2_tot, ns2 = swap_climb(prob, r2, cache, time.time() + 20)
        _, pb_r = S.total_obj(prob, r_asg, cache)
        _, pb_s = S.total_obj(prob, s2, cache)
        z2r, z3r = S.obj23(prob, r_asg); z2s, z3s = S.obj23(prob, s2)
        print(f"{name}: reloc-opt={r_tot:.0f} (Z3={z3r:.0f}) -> +swaps={s2_tot:.0f} (Z3={z3s:.0f}) "
              f"swaps_taken={nswap+ns2}  delta={100*(s2_tot-r_tot)/r_tot:+.1f}%"
              f"{'  <== SWAP LEVER' if s2_tot < r_tot - 1e-9 else ''}")


if __name__ == "__main__":
    main()
