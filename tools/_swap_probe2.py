"""Re-verify the swap lever on the STRONG baseline: the full BRIDGE pipeline (improved_search
+ LAHC + recombine, multi-order eval) converged assignment -- NOT the weak relocation-only climb
of _swap_probe. recombine already does set-partitioning over bay-pieces (a multi-block exchange),
so it may already capture the swap gain. If swaps STILL improve the recombined solution, the swap
move adds value the deployed solver lacks; if not, recombine already covers it.

Deterministic (SOLVER_MAX_EVALS). Run: ./.venv/Scripts/python.exe tools/_swap_probe2.py <E>
"""
from __future__ import annotations
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("SOLVER_MASK", "1")
os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
os.environ.setdefault("SOLVER_NUMBA", "1")
os.environ.setdefault("SOLVER_MULTIORDER", "1")
os.environ.setdefault("SOLVER_UNIFIED_ILS", "1")
os.environ.setdefault("SOLVER_LAHC", "1")
import solver as S           # noqa: E402
from packing import fits     # noqa: E402


def swap_climb(prob, assign, cache, deadline):
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
    E = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    INST = ["T20", "T14", "T13", "T11", "T18", "T9", "T6"]
    for name in INST:
        prob = json.load(open(ROOT / "train" / f"{name}.json"))
        os.environ["SOLVER_MAX_EVALS"] = str(E)
        # full pipeline (improved + LAHC + recombine, multi-order) -> converged assignment
        best, _base = S.framework_solve(prob, 9999.0, _return_assignment=True)
        # score the strong base on the same multi-order basis, then swap-climb (wall-mode cache)
        S._EVAL_LIMIT = None
        cache = {}
        base_tot, _ = S.total_obj(prob, best, cache)
        z2b, z3b = S.obj23(prob, best)
        s_asg, s_tot, nswap = swap_climb(prob, best, cache, time.time() + 60)
        z2s, z3s = S.obj23(prob, s_asg)
        print(f"{name}: strong-base(E={E})={base_tot:.0f} (Z3={z3b:.0f}) -> +swaps={s_tot:.0f} "
              f"(Z3={z3s:.0f}) swaps={nswap}  delta={100*(s_tot-base_tot)/base_tot:+.1f}%"
              f"{'  <== SWAP still helps' if s_tot < base_tot - 1e-9 else '  (recombine already covers)'}")


if __name__ == "__main__":
    main()
