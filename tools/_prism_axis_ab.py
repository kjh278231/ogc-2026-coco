"""Tier 1: isolate the L / R / fresh-restart worker axes via direct refine_anchor calls
(no portfolio, no multiprocessing). Fixed eval budget -> deterministic; same output style
as tools/_prism_ab.py. See docs/prism_worker_axis_diversity_design.md.

Usage: python tools/_prism_axis_ab.py <axis> <inst> [anchor=capped] [evals=4000]
       axis: baseline | L30 | diverse | diverse1 | diverse3 | R16 | R4 | guided
"""
import os, sys, json, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "prism"))

axis = sys.argv[1]
inst = sys.argv[2]
anchor_name_want = sys.argv[3] if len(sys.argv) > 3 else "capped"
evals = int(sys.argv[4]) if len(sys.argv) > 4 else 4000

# deployed PRISM config (myalgorithm.py) + CP_WORKERS=1 for determinism (as _prism_ab.py)
for k, v in (("SOLVER_MASK_SEARCH", "1"), ("SOLVER_MASK", "1"), ("SOLVER_NUMBA", "1"),
             ("SOLVER_MASK_PREPARE", "1"), ("SOLVER_MULTIORDER", "1"), ("SOLVER_SWAP", "1"),
             ("SOLVER_CP_WORKERS", "1")):
    os.environ.setdefault(k, v)

import prism_engine as P
K = P.K

prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
anchors = dict(P._anchors(prob, mip_tl=4.0, want_mip=True))
anchor_name = anchor_name_want if anchor_name_want in anchors else next(iter(anchors))
anchor = anchors[anchor_name]

# Explicit reset to baseline before applying the axis: refine_anchor does NOT reset
# K._MASK_R_SEARCH (module global) nor these env flags, so they would leak across
# iterations when sweeping several axes in one process.
K._MASK_R_SEARCH = 8
os.environ.pop("PRISM_REFINE_DIVERSE", None)
os.environ.pop("PRISM_REFINE_DIVERSE_EVERY", None)
os.environ.pop("SOLVER_GUIDED", None)

L, seed = 1, 20260629
if axis == "L30":
    L = 30
elif axis == "diverse":
    os.environ["PRISM_REFINE_DIVERSE"] = "1"
elif axis == "diverse1":
    os.environ.update(PRISM_REFINE_DIVERSE="1", PRISM_REFINE_DIVERSE_EVERY="1")
elif axis == "diverse3":
    os.environ.update(PRISM_REFINE_DIVERSE="1", PRISM_REFINE_DIVERSE_EVERY="3")
elif axis == "R16":
    K._MASK_R_SEARCH = 16
elif axis == "R4":
    K._MASK_R_SEARCH = 4
elif axis == "guided":
    os.environ["SOLVER_GUIDED"] = "1"
elif axis != "baseline":
    print("unknown axis", axis); sys.exit(1)

t0 = time.time()
best, pool, tot = P.refine_anchor(prob, anchor, timelimit=None, L=L, eval_limit=evals, seed=seed)
wall = time.time() - t0
obj, packed = K._score_and_pack(prob, best, poly_deadline=None)
z2, z3 = K.obj23(prob, best)
w = prob["weights"]
z1 = round((obj - w["w2"] * z2 - w["w3"] * z3) / w["w1"])
print(json.dumps({"axis": axis, "inst": inst, "anchor": anchor_name, "evals": evals,
                  "obj": round(obj), "z1z2z3": [z1, round(z2), round(z3)],
                  "pool": len(pool), "wall_s": round(wall, 1)}))
