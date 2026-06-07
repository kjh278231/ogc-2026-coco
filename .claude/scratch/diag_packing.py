"""2nd-priority diagnosis: is the residual tardiness on the tardy instances
PACKING-driven (AABB wastes space) or LOAD-driven (the assignment overloads a bay)?

Hold the framework's chosen assignment FIXED. Re-pack each bay two ways and compare
per-bay tardiness:
  - AABB-disjoint  (current solver)
  - polygon-disjoint (exact footprint; strictly more permissive, so T_poly <= T_aabb)
Delta = T_aabb - T_poly  == tardiness caused purely by AABB looseness.
  Delta large  -> packing-driven  -> adaptive polygon packing is worth it.
  Delta ~ 0    -> load-driven      -> tighter packing is useless; assignment is the lever.
"""
import sys, os, json, time, math
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline")); sys.path.insert(0, HERE)
import utils
from utils import Bay, Block, check_entry
import solver
from shapely.geometry import Polygon
from shapely.ops import unary_union


def footprint(blk):
    polys = []
    for layer in blk.layers_at_pos():
        if len(layer) >= 3:
            p = Polygon(layer)
            if not p.is_valid:
                p = p.buffer(0)
            polys.append(p)
    return unary_union(polys) if polys else None


def find_slot_poly(bay, present_objs, overlap_objs, bd, bid, W, H, step):
    ov = [(ob.bounding_rect(), footprint(ob)) for ob in overlap_objs]
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = solver.orient_bbox(bd, o)
        x_start = math.ceil(max(0.0, -mnx)); x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny)); y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        for y in range(y_start, y_end + 1, step):
            for x in range(x_start, x_end + 1, step):
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                cb = cand.bounding_rect()
                cfp = None
                bad = False
                for (ob_box, ob_fp) in ov:
                    if not (cb[0] < ob_box[2] and ob_box[0] < cb[2]
                            and cb[1] < ob_box[3] and ob_box[1] < cb[3]):
                        continue  # AABBs disjoint -> polygons disjoint, skip
                    if cfp is None:
                        cfp = footprint(cand)
                    if cfp is not None and ob_fp is not None and cfp.intersection(ob_fp).area > 1e-9:
                        bad = True
                        break
                if bad:
                    continue
                if check_entry(bay, present_objs, cand):  # boundary
                    continue
                return (x, y, o)
    return None


def solve_bay_poly(prob, j, ids, step=2, tcap=200):
    bays = prob["bays"]; blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j); W = bays[j]["width"]; H = bays[j]["height"]
    placed = []
    order = sorted(ids, key=lambda i: (blocks[i]["due_date"], blocks[i]["processing_time"]))
    for i in order:
        bd = blocks[i]; R = bd["release_time"]; P = bd["processing_time"]
        chosen = None
        for t in range(R, R + tcap):
            present = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                       for p in placed if p["entry"] <= t < p["exit"]]
            overlap = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                       for p in placed if p["entry"] < t + P and t < p["exit"]]
            slot = find_slot_poly(bay, present, overlap, bd, i, W, H, step)
            if slot:
                chosen = (t, slot[0], slot[1], slot[2]); break
        if chosen is None:
            t = max((p["exit"] for p in placed), default=R)
            o_fit = next((o for o in range(len(bd["shape"]))
                          if math.ceil(max(0.0, -solver.orient_bbox(bd, o)[0])) + solver.orient_bbox(bd, o)[2] <= W
                          and math.ceil(max(0.0, -solver.orient_bbox(bd, o)[1])) + solver.orient_bbox(bd, o)[3] <= H), 0)
            mnx, mny, _, _ = solver.orient_bbox(bd, o_fit)
            chosen = (t, math.ceil(max(0.0, -mnx)), math.ceil(max(0.0, -mny)), o_fit)
        placed.append({"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
                       "entry": chosen[0], "exit": chosen[0] + P})
    return placed


def run(path, tl=30):
    prob = json.load(open(path, encoding="utf-8"))
    name = os.path.basename(path).replace(".json", "")
    # framework's chosen assignment (from its solution)
    sol = solver.framework_solve(prob, tl)
    assign = {}
    for t_str, ops in sol["operations"].items():
        for op in ops:
            if op["type"] == "ENTRY":
                assign[op["block_id"]] = op["bay_id"]
    m = len(prob["bays"])
    print(f"\n{name}:")
    tot_a = tot_p = 0.0
    for j in range(m):
        ids = [i for i in assign if assign[i] == j]
        if not ids:
            continue
        Ta, _ = solver.extract_tardiness(prob, j, solver.solve_bay(prob, j, ids))
        Tp, _ = solver.extract_tardiness(prob, j, solve_bay_poly(prob, j, ids))
        tot_a += Ta; tot_p += Tp
        if Ta > 0 or Tp > 0:
            print(f"  bay{j}: n={len(ids):3d}  T_aabb={Ta:6.0f}  T_poly={Tp:6.0f}  Δ={Ta-Tp:6.0f}")
    verdict = "PACKING-driven" if (tot_a > 0 and tot_p < 0.5 * tot_a) else \
              ("LOAD-driven" if tot_a > 0 else "no tardiness")
    print(f"  TOTAL: T_aabb={tot_a:.0f}  T_poly={tot_p:.0f}  Δ={tot_a-tot_p:.0f}  [{verdict}]")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        run(p)
