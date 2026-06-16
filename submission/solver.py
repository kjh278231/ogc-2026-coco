"""Self-contained OGC 2026 solver (depends only on utils.py).

Framework: assignment search (scored by a per-bay footprint-disjoint admission
packer) + best-of non-regression guard. Crane extraction is free by construction
because no two temporally-overlapping blocks share a footprint (AABB-disjoint),
which is affordable since bay area utilisation is low. See docs/technical_report.md.

Entry point: framework_solve(prob, timelimit) -> solution dict. Time-managed so it
never exceeds `timelimit`; always returns a feasible solution (disjoint placement
+ guaranteed empty-bay fallback).
"""
from __future__ import annotations
import time
import math
import os
import random
import numpy as _np
from utils import Bay, Block, check_entry, check_exit

try:
    from shapely.geometry import Polygon as _ShapelyPolygon
    from shapely.ops import unary_union as _unary_union
    from shapely.affinity import translate as _translate
    import shapely as _shapely
    _HAS_SHAPELY = True
except Exception:                       # pragma: no cover
    _HAS_SHAPELY = False

try:
    from ortools.sat.python import cp_model as _cp_model
    _HAS_ORTOOLS = True
except Exception:                       # pragma: no cover
    _HAS_ORTOOLS = False

try:
    from numba import njit as _njit
    _HAS_NUMBA = True
except Exception:                       # pragma: no cover
    _HAS_NUMBA = False

# Optional numba-jitted AABB candidate scan (env-gated by SOLVER_NUMBA; OFF by default so
# the submission/default path stays bit-identical). The inner overlap loop of find_slot is
# ~97% of search time; jitting it yields many more candidate evaluations per wall-second.
# It returns the SAME first bottom-left free (x, y) as the pure-Python loop, so it is
# behaviour-invariant -- validate with SOLVER_MAX_EVALS (identical objective at the same
# eval count, only lower wall). Falls back to pure Python if numba is unavailable.
_NUMBA_ON = _HAS_NUMBA and bool(os.environ.get("SOLVER_NUMBA"))

if _HAS_NUMBA:
    @_njit(cache=True)
    def _aabb_scan(boxes, lbx0, lby0, lbx1, lby1, x_start, x_end, y_start, y_end,
                   step, start_y, start_x):
        """First bottom-left (x, y) (row-major: y outer, x inner, starting at
        (start_y, start_x)) whose candidate AABB overlaps no box in `boxes`
        (shape (K,4) = x0,y0,x1,y1). Returns (-1, -1) if none. Mirrors find_slot's
        overlap test exactly (same operands/inequalities) so the result is identical."""
        K = boxes.shape[0]
        y = start_y
        x = start_x
        while y <= y_end:
            cy0 = lby0 + y
            cy1 = lby1 + y
            while x <= x_end:
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                free = True
                for i in range(K):
                    if (cx0 < boxes[i, 2] and boxes[i, 0] < cx1
                            and cy0 < boxes[i, 3] and boxes[i, 1] < cy1):
                        free = False
                        break
                if free:
                    return x, y
                x += step
            x = x_start
            y += step
        return -1, -1

    @_njit(cache=True)
    def _masks_overlap_u64(a_rows, ay, a_h, a_words, b_rows, by, b_h, b_words, dx):
        """uint64-packed equivalent of masks_overlap: returns the SAME boolean as the
        pure-Python big-int version, faster (no per-row Python loop). a_rows/b_rows are
        (h, words) uint64 arrays; ay/by = world y of row 0; dx = ax - bx (column shift)."""
        y0 = ay if ay > by else by
        ya = ay + a_h
        yb = by + b_h
        y1 = ya if ya < yb else yb
        if y0 >= y1:
            return False
        if dx >= 0:
            ws = dx // 64
            bs = dx % 64
            for gy in range(y0, y1):
                ra = gy - ay
                rb = gy - by
                for w in range(a_words):
                    bw = w + ws
                    if bw >= b_words:
                        break
                    shifted = b_rows[rb, bw] >> bs
                    if bs != 0 and bw + 1 < b_words:
                        shifted = shifted | (b_rows[rb, bw + 1] << (64 - bs))
                    if a_rows[ra, w] & shifted:
                        return True
        else:
            dxx = -dx
            ws = dxx // 64
            bs = dxx % 64
            for gy in range(y0, y1):
                ra = gy - ay
                rb = gy - by
                for w in range(b_words):
                    aw = w + ws
                    if aw >= a_words:
                        break
                    shifted = a_rows[ra, aw] >> bs
                    if bs != 0 and aw + 1 < a_words:
                        shifted = shifted | (a_rows[ra, aw + 1] << (64 - bs))
                    if b_rows[rb, w] & shifted:
                        return True
        return False

# Pool of (bay, block_set) -> AABB tardiness pieces seen during the search, for the
# Z2-aware set-partitioning recombination final step.
_POOL: dict = {}

# Cache of local (reference-anchored) footprints, keyed by (id(block_data), orient).
# A block placed at (x,y) has world footprint = translate(local, x, y), so the
# expensive polygon build happens once per (block, orientation) and translation is
# a cheap affine transform.
_LOCAL_FP: dict = {}

# Cache of the local (origin-anchored) AABB, keyed by (id(block_data), orient).
# A block placed at (x,y) has bounding_rect = this box translated by (x,y) (the
# bbox is translation-equivariant), so the candidate box in the inner packing
# loop is pure arithmetic -- no Block need be built per candidate just to read it.
_LOCAL_BOX: dict = {}


def _local_box(bd, o):
    """Origin-anchored AABB (min_x,min_y,max_x,max_y) for (block, orientation).
    Equals Block(bd, 0, 0, o).bounding_rect(); cached per (block, orientation)."""
    key = (id(bd), o)
    box = _LOCAL_BOX.get(key)
    if box is None:
        box = Block(block_id=-1, block_data=bd, x=0, y=0, orient_idx=o).bounding_rect()
        _LOCAL_BOX[key] = box
    return box

# Search budgeting. By default the searches stop on a wall-clock DEADLINE (used for
# the real submission). For reproducible A/B EVALUATION, set SOLVER_MAX_EVALS: the
# searches then stop after a fixed number of candidate evaluations (total_obj calls)
# -> deterministic, no wall-clock variance. In that mode each `deadline` argument is
# reinterpreted as an eval-count threshold. Either way the harness should report the
# wall-clock time per problem (eval mode does NOT bound time -- it must be watched).
_EVALS = 0
_EVAL_LIMIT = None

# Anytime incumbent trace (analysis only, env-gated by SOLVER_TRACE; default off so the
# submission path is untouched). When on, total_obj appends (elapsed_s, objective) each
# time it sees a new global best, giving an x=time / y=objective curve for one run.
_TRACE: list = []
_TRACE_ON = False
_TRACE_T0 = 0.0
_TRACE_BEST = float("inf")


def _now():
    return _EVALS if _EVAL_LIMIT is not None else time.time()


def _within(x):
    """True while the search may continue. `x` is a time (default) or, in eval
    mode, an eval-count threshold."""
    return (_EVALS < x) if _EVAL_LIMIT is not None else (time.time() < x)


def _mid(deadline):
    """Halfway point between now and `deadline`, in the active unit (time/evals)."""
    n = _now()
    return n + (deadline - n) * 0.5


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
# Cache of the raw-vertex AABB per (id(block_data), orient); pure function of the
# shape, cleared per solve (id()-keyed) like the other caches.
_ORIENT_BBOX: dict = {}


def orient_bbox(bd, o):
    key = (id(bd), o)
    box = _ORIENT_BBOX.get(key)
    if box is None:
        xs = []
        ys = []
        for layer in bd["shape"][o]["layers"]:
            for x, y in layer:
                xs.append(x)
                ys.append(y)
        box = (min(xs), min(ys), max(xs), max(ys))
        _ORIENT_BBOX[key] = box
    return box


def fits(bd, bay):
    """An integer-reference placement keeps the block fully inside the bay."""
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        if (math.ceil(max(0.0, -mnx)) + mxx <= bay["width"]
                and math.ceil(max(0.0, -mny)) + mxy <= bay["height"]):
            return True
    return False


# --------------------------------------------------------------------------- #
# per-bay footprint-disjoint admission packer
# --------------------------------------------------------------------------- #
def find_slot(bay, present_objs, overlap_objs, bd, bid, W, H, step):
    """Earliest bottom-left feasible (x, y, o): in-bounds, crane-entry OK, and
    full-footprint (AABB) disjoint from all temporally-overlapping blocks
    (=> no cross-layer overlap => never crane-trapped)."""
    ov_boxes = [ob.bounding_rect() for ob in overlap_objs]
    if _NUMBA_ON:
        boxes_arr = (_np.asarray(ov_boxes, dtype=_np.float64) if ov_boxes
                     else _np.empty((0, 4), dtype=_np.float64))
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        # integer range: lower=ceil (else min vertex < 0), upper=floor (else > W)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        # candidate box = origin box translated by (x, y); arithmetic only, so we
        # build a Block only for the few candidates that survive the overlap reject
        # and need the (boundary) crane-entry check.
        lbx0, lby0, lbx1, lby1 = _local_box(bd, o)
        if _NUMBA_ON:
            # The jitted scan returns successive first-free candidates in the same
            # bottom-left (row-major) order; Python runs the rare boundary crane-entry
            # check and resumes past a rejected candidate, so the chosen (x, y, o) is
            # identical to the pure-Python loop below -- only faster.
            sy, sx = y_start, x_start
            while sy <= y_end:
                rx, ry = _aabb_scan(boxes_arr, float(lbx0), float(lby0), float(lbx1),
                                    float(lby1), x_start, x_end, y_start, y_end,
                                    step, sy, sx)
                if rx < 0:
                    break
                cand = Block(block_id=bid, block_data=bd, x=rx, y=ry, orient_idx=o)
                if not check_entry(bay, present_objs, cand):
                    return (rx, ry, o)
                sx = rx + step
                sy = ry
                if sx > x_end:
                    sx = x_start
                    sy = ry + step
            continue
        for y in range(y_start, y_end + 1, step):
            cy0 = lby0 + y
            cy1 = lby1 + y
            # the y-half of the overlap test is constant across the row, so filter
            # to the y-overlapping boxes once; the inner x-loop then checks only the
            # x-half over a much smaller list (usually empty => whole row is free).
            row = [b for b in ov_boxes if cy0 < b[3] and b[1] < cy1]
            for x in range(x_start, x_end + 1, step):
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                if any(cx0 < b[2] and b[0] < cx1 for b in row):
                    continue
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                if check_entry(bay, present_objs, cand):  # boundary only now
                    continue
                return (x, y, o)
    return None


def _local_footprint(bd, o):
    """Footprint of the block at orientation o anchored at (0,0); cached so the
    Shapely build runs once per (block, orientation)."""
    key = (id(bd), o)
    fp = _LOCAL_FP.get(key, 0)
    if fp != 0:
        return fp
    polys = []
    for layer in Block(block_id=-1, block_data=bd, x=0, y=0, orient_idx=o).layers_at_pos():
        if len(layer) >= 3:
            p = _ShapelyPolygon(layer)
            if not p.is_valid:
                p = p.buffer(0)
            polys.append(p)
    fp = _unary_union(polys) if polys else None
    _LOCAL_FP[key] = fp
    return fp


def _block_footprint(bd, x, y, o):
    """World footprint = local footprint translated by (x, y) (cheap affine)."""
    loc = _local_footprint(bd, o)
    return _translate(loc, x, y) if loc is not None else None


# --------------------------------------------------------------------------- #
# Bitmask collision model (env-gated; see docs/bitmask_collision_design.md).
# A conservative SUPERCOVER of the polygon footprint on an integer grid (R cells per
# bay unit): mask-disjoint => polygon-disjoint (never under-rejects -> feasibility is
# preserved), but tighter than AABB (recovers placements AABB over-rejects). Cheap
# integer row-bitset overlap test, intended to approach AABB speed while approaching
# polygon quality. Default path is unchanged until SOLVER_MASK is set.
# --------------------------------------------------------------------------- #
_LOCAL_MASK: dict = {}

# Use the supercover mask (instead of AABB) for the SEARCH's per-bay packing/scoring
# (env SOLVER_MASK_SEARCH). This makes the proxy near-polygon-accurate -> directly
# attacks proxy-drift (search optimises ~the true objective) and explores tight
# interlocking placements AABB rejects, at ~3-4x AABB pack cost. The final-build mask
# is the separate SOLVER_MASK gate. Default off -> default search path unchanged.
_MASK_SEARCH = _HAS_SHAPELY and bool(os.environ.get("SOLVER_MASK_SEARCH"))
_MASK_R_SEARCH = int(os.environ.get("SOLVER_MASK_SEARCH_R", "8"))
# SOLVER_MASK_PREPARE: shapely.prepare the buffer before the per-cell point-membership
# test in _local_mask -> ~4.5x faster mask build (the point test is ~92% of build cost),
# BIT-IDENTICAL mask (0 mismatch validated) so feasibility is untouched. Gated OFF by
# default: at a fixed eval count the objective is unchanged, but in WALL mode the freed
# build time lets the search run further -- which is net -4.8% on train BUT exposes the
# search's non-monotonicity (proxy-drift: prob_12 +38.8%, prob_15 +35.6%). Keep off until
# the snapshot true-scoring guard makes "more search never hurts"; then flip it on.
_MASK_PREPARE = _HAS_SHAPELY and bool(os.environ.get("SOLVER_MASK_PREPARE"))


class MaskProxy:
    __slots__ = ("R", "ix0", "iy0", "width_bits", "height_rows", "rows",
                 "width_words", "rows_u64")

    def __init__(self, R, ix0, iy0, width_bits, height_rows, rows, width_words, rows_u64):
        self.R = R
        self.ix0 = ix0            # local grid x offset (cell index of bit 0 of each row)
        self.iy0 = iy0            # local grid y offset (cell index of rows[0])
        self.width_bits = width_bits
        self.height_rows = height_rows
        self.rows = rows          # tuple[int]: row bitsets, bit k = cell (ix0 + k)
        self.width_words = width_words   # uint64 words/row (for the numba overlap test)
        self.rows_u64 = rows_u64         # (height_rows, width_words) uint64 packing of rows


def _local_mask(bd, o, R):
    """Supercover bitmask of the (block, orientation) union footprint at the local
    origin, on a grid of R cells per bay unit. A cell is occupied iff its center lies
    within footprint.buffer(sqrt(2)/(2R)+eps); since every point of a cell is within
    sqrt(2)/(2R) of its center, this marks EVERY cell the true polygon touches ->
    mask is a superset of the footprint -> mask-disjoint implies polygon-disjoint.
    Cached per (block, orientation, R)."""
    key = (id(bd), o, R)
    m = _LOCAL_MASK.get(key)
    if m is not None:
        return m
    fp = _local_footprint(bd, o)
    if fp is None or fp.is_empty:
        m = MaskProxy(R, 0, 0, 0, 0, (), 0, _np.zeros((0, 0), dtype=_np.uint64))
        _LOCAL_MASK[key] = m
        return m
    d = math.sqrt(2.0) / (2.0 * R) + 1e-9
    buf = fp.buffer(d)
    # Prepare the buffer once so the per-cell point-membership test below uses GEOS's
    # indexed representation instead of a full intersects per point. The point-membership
    # step is ~92% of mask-build cost; prepare cuts it ~78% (4.5x) with BIT-IDENTICAL
    # results (validated: 0 mismatch over all (block,orient) of prob_13/14/20) -> the mask
    # is unchanged so the supercover/feasibility guarantee is fully preserved. Gated
    # (SOLVER_MASK_PREPARE) because the freed wall time amplifies search non-monotonicity.
    if _MASK_PREPARE:
        _shapely.prepare(buf)
    minx, miny, maxx, maxy = buf.bounds
    ix0 = math.floor(minx * R) - 1
    iy0 = math.floor(miny * R) - 1
    ix1 = math.floor(maxx * R) + 1
    iy1 = math.floor(maxy * R) + 1
    width_bits = ix1 - ix0 + 1
    height_rows = iy1 - iy0 + 1
    gxs = _np.arange(ix0, ix1 + 1)
    gys = _np.arange(iy0, iy1 + 1)
    cx = (gxs + 0.5) / R
    cy = (gys + 0.5) / R
    CX, CY = _np.meshgrid(cx, cy)                      # (height_rows, width_bits)
    # intersects (interior OR boundary) is the generous/safe choice: extra occupied
    # cells only over-reject; a missed cell would be a feasibility-breaking false neg.
    inside = _shapely.intersects(buf, _shapely.points(CX.ravel(), CY.ravel()))
    inside = inside.reshape(CY.shape)
    rows = []
    for r in range(height_rows):
        rb = 0
        for k in _np.nonzero(inside[r])[0]:
            rb |= (1 << int(k))
        rows.append(rb)
    # uint64 packing of the same rows for the numba overlap test (bit-identical content)
    width_words = (width_bits + 63) // 64
    rows_u64 = _np.zeros((height_rows, width_words), dtype=_np.uint64)
    _m64 = (1 << 64) - 1
    for r in range(height_rows):
        rb = rows[r]
        for w in range(width_words):
            rows_u64[r, w] = (rb >> (64 * w)) & _m64
    m = MaskProxy(R, ix0, iy0, width_bits, height_rows, tuple(rows), width_words, rows_u64)
    _LOCAL_MASK[key] = m
    return m


def masks_overlap(a, ax, ay, b, bx, by):
    """True iff supercover masks a, b overlap, given their WORLD grid offsets
    (ax, ay) and (bx, by) (= local ix0/iy0 + placement*R). Row bitsets aligned by an
    integer column shift dx = ax - bx. When SOLVER_NUMBA is on, dispatch to the
    uint64-packed numba version (same boolean, no per-row Python loop)."""
    if _NUMBA_ON and a.width_words and b.width_words:
        return bool(_masks_overlap_u64(
            a.rows_u64, ay, a.height_rows, a.width_words,
            b.rows_u64, by, b.height_rows, b.width_words, ax - bx))
    y0 = max(ay, by)
    y1 = min(ay + a.height_rows, by + b.height_rows)
    if y0 >= y1:
        return False
    dx = ax - bx
    arows = a.rows
    brows = b.rows
    if dx >= 0:
        for gy in range(y0, y1):
            if arows[gy - ay] & (brows[gy - by] >> dx):
                return True
    else:
        ndx = -dx
        for gy in range(y0, y1):
            if (arows[gy - ay] >> ndx) & brows[gy - by]:
                return True
    return False


def find_slot_poly(bay, present_objs, ov_boxfps, bd, bid, W, H, step):
    """Like find_slot but disjointness is the EXACT footprint (polygon), which is
    strictly more permissive than AABB (packs tighter). AABB is used only as a
    cheap pre-filter. `ov_boxfps` = precomputed [(bbox, footprint)] of the
    temporally-overlapping placed blocks (footprints cached, not rebuilt here)."""
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        lbx0, lby0, lbx1, lby1 = _local_box(bd, o)  # candidate box = this + (x, y)
        for y in range(y_start, y_end + 1, step):
            cy0 = lby0 + y
            cy1 = lby1 + y
            # y-half of the AABB pre-filter is constant across the row; filter once.
            row = [bf for bf in ov_boxfps if cy0 < bf[0][3] and bf[0][1] < cy1]
            for x in range(x_start, x_end + 1, step):
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                cfp = None
                bad = False
                for (ob_box, ob_fp) in row:
                    if not (cx0 < ob_box[2] and ob_box[0] < cx1):
                        continue  # AABBs disjoint => footprints disjoint
                    if cfp is None:
                        cfp = _block_footprint(bd, x, y, o)
                    if cfp is not None and ob_fp is not None and cfp.intersection(ob_fp).area > 1e-9:
                        bad = True
                        break
                if bad:
                    continue
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                if check_entry(bay, present_objs, cand):
                    continue
                return (x, y, o)
    return None


def find_slot_mask(bay, present_objs, ov_boxmasks, bd, bid, W, H, step, R):
    """Like find_slot_poly but disjointness is the SUPERCOVER bitmask instead of the
    exact polygon: more permissive than AABB (packs tighter), conservative vs polygon
    (mask-disjoint => polygon-disjoint, so feasibility holds), and far cheaper than
    shapely. `ov_boxmasks` = [(bbox, MaskProxy, world_ix0, world_iy0)] of the
    temporally-overlapping placed blocks; AABB is the cheap pre-filter (AABB-disjoint
    => footprints disjoint => safe, since footprint <= AABB)."""
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        x_start = math.ceil(max(0.0, -mnx))
        x_end = math.floor(W - mxx)
        y_start = math.ceil(max(0.0, -mny))
        y_end = math.floor(H - mxy)
        if x_end < x_start or y_end < y_start:
            continue
        lbx0, lby0, lbx1, lby1 = _local_box(bd, o)
        cmask = _local_mask(bd, o, R)
        for y in range(y_start, y_end + 1, step):
            cy0 = lby0 + y
            cy1 = lby1 + y
            cand_ay = cmask.iy0 + y * R
            row = [bm for bm in ov_boxmasks if cy0 < bm[0][3] and bm[0][1] < cy1]
            for x in range(x_start, x_end + 1, step):
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                cand_ax = cmask.ix0 + x * R
                bad = False
                for (ob_box, ob_mask, ob_mix0, ob_miy0) in row:
                    if not (cx0 < ob_box[2] and ob_box[0] < cx1):
                        continue  # AABBs disjoint => footprints disjoint => safe
                    if masks_overlap(cmask, cand_ax, cand_ay, ob_mask, ob_mix0, ob_miy0):
                        bad = True
                        break
                if bad:
                    continue
                cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
                if check_entry(bay, present_objs, cand):
                    continue
                return (x, y, o)
    return None


def solve_bay(prob, j, ids, step=2, tcap=200, poly=False, deadline=None, mask=False, mask_R=8):
    """Per-bay footprint-disjoint admission packer. If poly=True, escalate to the
    exact polygon-disjoint check whenever cheap AABB finds no slot at a time t
    (recovers packing-driven tardiness; pays the shapely cost only where needed).
    Once `deadline` (wall-clock) passes, poly escalation is dropped so the pack
    always finishes in bounded time (AABB-only is the validated-feasible floor)."""
    use_poly = poly and _HAS_SHAPELY
    use_mask = mask and _HAS_SHAPELY
    bays = prob["bays"]
    blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j)
    W = bays[j]["width"]
    H = bays[j]["height"]
    placed = []
    order = sorted(ids, key=lambda i: (blocks[i]["due_date"], blocks[i]["processing_time"]))
    for i in order:
        bd = blocks[i]
        R = bd["release_time"]
        P = bd["processing_time"]
        chosen = None
        for t in range(R, R + tcap):
            present = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                       for p in placed if p["entry"] <= t < p["exit"]]
            overlap = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                       for p in placed if p["entry"] < t + P and t < p["exit"]]
            slot = find_slot(bay, present, overlap, bd, i, W, H, step)
            if slot is None and use_mask:
                if deadline is not None and time.time() > deadline:
                    use_mask = False  # out of time: revert to AABB for the rest
                else:
                    ov_boxmasks = [(p["bb"], p["mask"], p["mix0"], p["miy0"]) for p in placed
                                   if p["entry"] < t + P and t < p["exit"]]
                    slot = find_slot_mask(bay, present, ov_boxmasks, bd, i, W, H, step, mask_R)
            elif slot is None and use_poly:
                if deadline is not None and time.time() > deadline:
                    use_poly = False  # out of time: revert to AABB for the rest
                else:
                    ov_boxfps = [(p["bb"], p["fp"]) for p in placed
                                 if p["entry"] < t + P and t < p["exit"]]
                    slot = find_slot_poly(bay, present, ov_boxfps, bd, i, W, H, step)
            if slot:
                chosen = (t, slot[0], slot[1], slot[2])
                break
        if chosen is None:
            # fallback: search forward from when the bay empties, then a fitting
            # orientation at its in-bounds corner (boundary + collision safe).
            t = max((p["exit"] for p in placed), default=R)
            for tt in range(t, t + 1000):
                present = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                           for p in placed if p["entry"] <= tt < p["exit"]]
                overlap = [Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"])
                           for p in placed if p["entry"] < tt + P and tt < p["exit"]]
                slot = find_slot(bay, present, overlap, bd, i, W, H, step)
                if slot:
                    chosen = (tt, slot[0], slot[1], slot[2])
                    break
            if chosen is None:
                o_fit = next((o for o in range(len(bd["shape"]))
                              if math.ceil(max(0.0, -orient_bbox(bd, o)[0])) + orient_bbox(bd, o)[2] <= W
                              and math.ceil(max(0.0, -orient_bbox(bd, o)[1])) + orient_bbox(bd, o)[3] <= H), 0)
                mnx, mny, _, _ = orient_bbox(bd, o_fit)
                chosen = (t, math.ceil(max(0.0, -mnx)), math.ceil(max(0.0, -mny)), o_fit)
        rec = {"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
               "entry": chosen[0], "exit": chosen[0] + P}
        if use_mask:  # cache bbox + local mask + world grid offsets for later mask checks
            blk_o = Block(i, bd, chosen[1], chosen[2], chosen[3])
            rec["bb"] = blk_o.bounding_rect()
            lm = _local_mask(bd, chosen[3], mask_R)
            rec["mask"] = lm
            rec["mix0"] = lm.ix0 + chosen[1] * mask_R
            rec["miy0"] = lm.iy0 + chosen[2] * mask_R
        elif use_poly:  # cache footprint + bbox so later poly checks never rebuild
            blk_o = Block(i, bd, chosen[1], chosen[2], chosen[3])
            rec["bb"] = blk_o.bounding_rect()
            rec["fp"] = _block_footprint(bd, chosen[1], chosen[2], chosen[3])
        placed.append(rec)
    return placed


def extract_tardiness(prob, j, placed):
    """Greedy ASAP extraction (exit when processing done + crane-untrapped)."""
    bays = prob["bays"]
    blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j)
    blk = {p["id"]: Block(p["id"], blocks[p["id"]], p["x"], p["y"], p["o"]) for p in placed}
    entry = {p["id"]: p["entry"] for p in placed}
    proc = {p["id"]: blocks[p["id"]]["processing_time"] for p in placed}
    due = {p["id"]: blocks[p["id"]]["due_date"] for p in placed}
    ids = list(blk)
    horizon = max(entry[i] + proc[i] for i in ids) + len(ids) + 5
    present = set()
    exited = {}
    pend = sorted(ids, key=lambda i: entry[i])
    pe = 0
    for t in range(0, horizon + 1):
        changed = True
        while changed:
            changed = False
            for i in list(present):
                if t >= entry[i] + proc[i]:
                    if not check_exit(bay, [blk[k] for k in present], blk[i]):
                        exited[i] = t
                        present.discard(i)
                        changed = True
        while pe < len(pend) and entry[pend[pe]] <= t:
            present.add(pend[pe])
            pe += 1
        if not present and pe >= len(pend):
            break
    for i in ids:
        exited.setdefault(i, horizon)
    tard = sum(max(0.0, exited[i] - due[i]) for i in ids)
    return tard, exited


# --------------------------------------------------------------------------- #
# objective evaluation (Z2/Z3 instant; Z1 via per-bay packing, cached)
# --------------------------------------------------------------------------- #
def obj23(prob, assign):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    loads = [0.0] * m
    obj3 = 0.0
    for i, j in assign.items():
        loads[j] += blocks[i]["workload"]
        obj3 += max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    obj2 = math.floor(max((abs(u[a] * loads[a] - u[b] * loads[b])
                           for a in range(m) for b in range(m) if a != b), default=0.0))
    return obj2, obj3


def eval_obj1(prob, assign, cache):
    m = len(prob["bays"])
    obj1 = 0.0
    perbay = {}
    for j in range(m):
        ids = tuple(sorted(i for i, a in assign.items() if a == j))
        if not ids:
            perbay[j] = 0.0
            continue
        if ids in cache:
            perbay[j] = cache[ids]
        else:
            T, _ = extract_tardiness(prob, j, solve_bay(
                prob, j, list(ids), mask=_MASK_SEARCH, mask_R=_MASK_R_SEARCH))
            cache[ids] = T
            perbay[j] = T
        _POOL[(j, ids)] = perbay[j]   # record (bay, set) piece for recombination
        obj1 += perbay[j]
    return obj1, perbay


def total_obj(prob, assign, cache):
    global _EVALS, _TRACE_BEST
    _EVALS += 1
    obj1, perbay = eval_obj1(prob, assign, cache)
    obj2, obj3 = obj23(prob, assign)
    w = prob["weights"]
    tot = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    if _TRACE_ON and tot < _TRACE_BEST:
        _TRACE_BEST = tot
        _TRACE.append((time.time() - _TRACE_T0, tot))
    return tot, perbay


# --------------------------------------------------------------------------- #
# assignment heuristics
# --------------------------------------------------------------------------- #
def a_pref(prob):
    blocks = prob["blocks"]
    bays = prob["bays"]
    asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        asg[i] = next((j for j in order if fits(b, bays[j])), order[0])
    return asg


def a_balanced_load(prob):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    loads = [0.0] * m
    asg = {}
    for i in sorted(range(len(blocks)), key=lambda i: -blocks[i]["workload"]):
        b = blocks[i]
        cand = [j for j in range(m) if fits(b, bays[j])] or list(range(m))
        j = min(cand, key=lambda j: u[j] * (loads[j] + b["workload"]))
        asg[i] = j
        loads[j] += b["workload"]
    return asg


def a_pref_capped(prob, cap_factor=1.15):
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    cap = math.ceil(len(blocks) / m * cap_factor)
    cnt = [0] * m
    asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        order = [j for j in order if fits(b, bays[j])] or order
        j = next((j for j in order if cnt[j] < cap), order[0])
        asg[i] = j
        cnt[j] += 1
    return asg


# --------------------------------------------------------------------------- #
# searches (deadline-driven; only accept improving moves)
# --------------------------------------------------------------------------- #
def local_search(prob, assign, cache, deadline, patience=None):
    """Focused hill climb: move blocks out of tardy bays (first improvement).
    With `patience` set (unified ILS loop), stop after that many CONSECUTIVE
    non-improving candidate evaluations -- a timing-independent convergence stop --
    keeping `deadline` only as a hard safety cap. patience=None preserves the legacy
    behaviour exactly (deadline-driven full-sweep convergence)."""
    m = len(prob["bays"])
    blocks = prob["blocks"]
    best = dict(assign)
    best_tot, perbay = total_obj(prob, best, cache)
    improved = True
    noimp = 0
    while improved and _within(deadline):
        improved = False
        tardy = [j for j in range(m) if perbay.get(j, 0) > 0]
        movers = [i for i in best if best[i] in tardy]
        for i in movers:
            if not _within(deadline):
                break
            for j in range(m):
                if j == best[i] or not fits(blocks[i], prob["bays"][j]):
                    continue
                trial = dict(best)
                trial[i] = j
                tot, _ = total_obj(prob, trial, cache)
                if tot < best_tot - 1e-9:
                    best, best_tot = trial, tot
                    _, perbay = total_obj(prob, best, cache)
                    improved = True
                    noimp = 0
                    break
                elif patience is not None:
                    noimp += 1
                    if noimp >= patience:
                        return best, best_tot
    return best, best_tot


def improved_search(prob, cache, deadline):
    """Two-phase hill climb from the best heuristic seed. Phase 1 (first half):
    movers in tardy bays (Z1) + the max-(u*load) bay (Z2). Phase 2: also move
    blocks off their preferred bay (Z3), tried preference-first."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur, cur_tot = None, float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob)
        tot, _ = total_obj(prob, a, cache)
        if tot < cur_tot:
            cur, cur_tot = dict(a), tot
    _, perbay = total_obj(prob, cur, cache)

    def hillclimb(include_offpref, sub_deadline):
        nonlocal cur, cur_tot, perbay
        improved = True
        while improved and _within(sub_deadline):
            improved = False
            loads = [0.0] * m
            for i, j in cur.items():
                loads[j] += blocks[i]["workload"]
            maxload = max(range(m), key=lambda j: u[j] * loads[j])
            tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
            movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                      or (include_offpref and cur[i] != pref_bay[i])]
            for i in movers:
                if not _within(sub_deadline):
                    break
                targets = sorted((j for j in range(m)
                                  if j != cur[i] and fits(blocks[i], bays[j])),
                                 key=lambda j: -blocks[i]["bay_preferences"][j])
                for j in targets:
                    trial = dict(cur)
                    trial[i] = j
                    tot, _ = total_obj(prob, trial, cache)
                    if tot < cur_tot - 1e-9:
                        cur, cur_tot = trial, tot
                        _, perbay = total_obj(prob, cur, cache)
                        improved = True
                        break

    hillclimb(False, _mid(deadline))
    hillclimb(True, deadline)
    return cur, cur_tot


def _climb(prob, assign, cache, deadline, patience=None):
    """Hill climb to convergence-or-deadline: move blocks in tardy bays (Z1), the
    max-(u*load) bay (Z2), or off their preferred bay (Z3); targets preference-first.
    With `patience` set (unified ILS loop), stop after that many CONSECUTIVE
    non-improving candidate evaluations (timing-independent convergence stop),
    `deadline` kept only as a hard safety cap. patience=None == legacy behaviour."""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]
    avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur = dict(assign)
    cur_tot, perbay = total_obj(prob, cur, cache)
    improved = True
    noimp = 0
    while improved and _within(deadline):
        improved = False
        loads = [0.0] * m
        for i, j in cur.items():
            loads[j] += blocks[i]["workload"]
        maxload = max(range(m), key=lambda j: u[j] * loads[j])
        tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
        movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                  or cur[i] != pref_bay[i]]
        for i in movers:
            if not _within(deadline):
                break
            targets = sorted((j for j in range(m)
                              if j != cur[i] and fits(blocks[i], bays[j])),
                             key=lambda j: -blocks[i]["bay_preferences"][j])
            for j in targets:
                trial = dict(cur)
                trial[i] = j
                tot, _ = total_obj(prob, trial, cache)
                if tot < cur_tot - 1e-9:
                    cur, cur_tot = trial, tot
                    _, perbay = total_obj(prob, cur, cache)
                    improved = True
                    noimp = 0
                    break
                elif patience is not None:
                    noimp += 1
                    if noimp >= patience:
                        return cur, cur_tot
    return cur, cur_tot


def _perturb(prob, best, cache, rng):
    """One ILS kick: re-home k in [2,5] blocks (random repair). With SOLVER_GUIDED,
    DESTROY contributing blocks -- in a tardy bay [Z1], the max-(u*load) bay [Z2], or
    off their preferred bay [Z3]; 'mix' uses guided on 50% of kicks. Returns the
    perturbed assignment. (rng call sequence identical to the previous inline version.)"""
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    ids = list(best)
    k = rng.randint(2, 5)
    cand = dict(best)
    guided = os.environ.get("SOLVER_GUIDED")   # "1"=always guided, "mix"=50/50
    use_g = guided and (guided != "mix" or rng.random() < 0.5)
    if use_g:
        areas = [b["width"] * b["height"] for b in bays]
        u = [sum(areas) / m / a for a in areas]
        pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                    for i in range(len(blocks))}
        _, perbay = total_obj(prob, best, cache)
        loads = [0.0] * m
        for i, j in best.items():
            loads[j] += blocks[i]["workload"]
        maxload = max(range(m), key=lambda j: u[j] * loads[j])
        tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
        pool = [i for i in ids if best[i] in tardy or best[i] == maxload
                or best[i] != pref_bay[i]]
        chosen = rng.sample(pool, min(k, len(pool))) if pool else rng.sample(ids, min(k, len(ids)))
    else:
        chosen = [rng.choice(ids) for _ in range(k)]
    for i in chosen:
        opts = [j for j in range(m) if j != cand[i] and fits(blocks[i], bays[j])]
        if opts:
            cand[i] = rng.choice(opts)
    return cand


def _ils(prob, best, best_tot, cache, deadline, rng):
    """Iterated local search on the IDLE budget left after the main search converges:
    perturb the incumbent (see _perturb) and re-optimise (_climb), keeping the global
    best. SOLVER_GUIDED steers the perturbation; repair stays randomized for diversity."""
    while _within(deadline):
        cand = _perturb(prob, best, cache, rng)
        cur, tot = _climb(prob, cand, cache, deadline)
        if tot < best_tot - 1e-9:
            best, best_tot = cur, tot
    return best, best_tot


def _bestof_obj(prob, assign, deadline=None):
    """Full objective on the same per-bay best-of(AABB, polygon) basis the final
    build uses: w1·Σ min(T_aabb,T_poly) + w2·Z2 + w3·Z3. Used to guard the
    recombination adoption (Pareto-safe)."""
    w = prob["weights"]
    m = len(prob["bays"])
    o1 = 0.0
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        Ta, _ = extract_tardiness(prob, j, solve_bay(prob, j, ids, poly=False))
        Tp, _ = extract_tardiness(prob, j, solve_bay(prob, j, ids, poly=True, deadline=deadline))
        o1 += min(Ta, Tp)
    o2, o3 = obj23(prob, assign)
    return w["w1"] * o1 + w["w2"] * o2 + w["w3"] * o3


def _recombine(prob, best, deadline):
    """Z2-aware set-partitioning recombination of the cached (bay,set) pieces.
    Local search moves one block at a time and cannot recombine whole bay-pieces
    across solutions; the MIP can. column cost = w1·tardiness + w3·preference,
    global term = w2·Z2 (min-max, linearized). Hinted with the incumbent (so a
    time cut-off still yields >= incumbent on the MIP metric) and adopted only if
    the full best-of objective actually improves -- Pareto-safe."""
    if not _HAS_ORTOOLS:
        return best
    blocks = prob["blocks"]
    bays = prob["bays"]
    m = len(bays)
    n = len(blocks)
    w = prob["weights"]
    SCALE = 100
    cols = [(j, ids, T) for (j, ids), T in _POOL.items() if ids]
    if not cols:
        return best
    model = _cp_model.CpModel()
    x = [model.NewBoolVar(f"c{k}") for k in range(len(cols))]
    by_block = [[] for _ in range(n)]
    by_bay = [[] for _ in range(m)]
    wl = []
    cost = []
    for k, (j, ids, T) in enumerate(cols):
        for i in ids:
            by_block[i].append(k)
        by_bay[j].append(k)
        wl.append(sum(blocks[i]["workload"] for i in ids))
        pl = sum(max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j] for i in ids)
        cost.append(int(SCALE * (w["w1"] * T + w["w3"] * pl)))
    for i in range(n):
        if not by_block[i]:
            return best
        model.Add(sum(x[k] for k in by_block[i]) == 1)
    for j in range(m):
        if by_bay[j]:
            model.Add(sum(x[k] for k in by_bay[j]) <= 1)
    obj = sum(x[k] * cost[k] for k in range(len(cols)))
    if m >= 2:
        areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
        cj = [round(avg / areas[j] * SCALE) for j in range(m)]
        sload = [sum(cj[j] * wl[k] * x[k] for k in by_bay[j]) for j in range(m)]
        M = model.NewIntVar(0, 10 ** 12, "M")     # = SCALE * Z2
        for a in range(m):
            for b in range(m):
                if a != b:
                    model.Add(M >= sload[a] - sload[b])
        obj = obj + w["w2"] * M
    model.Minimize(obj)
    inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
           for j in range(m) if any(best[i] == j for i in best)}
    for k, (j, ids, T) in enumerate(cols):
        model.AddHint(x[k], 1 if (j, ids) in inc else 0)
    cp = _cp_model.CpSolver()
    cp.parameters.max_time_in_seconds = max(0.5, (deadline - time.time()) * 0.5)
    # default 4 workers; a parallel portfolio sets SOLVER_CP_WORKERS=1 so N chains use
    # N*1 cores (no >4-thread over-subscription under the 4-core cpulimit).
    cp.parameters.num_search_workers = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
    st = cp.Solve(model)
    if st not in (_cp_model.OPTIMAL, _cp_model.FEASIBLE):
        return best
    A = {}
    for k, (j, ids, T) in enumerate(cols):
        if cp.Value(x[k]) == 1:
            for i in ids:
                A[i] = j
    if len(A) != n:
        return best
    # best-of full-objective guard (Pareto-safe): adopt only if A truly improves.
    if _bestof_obj(prob, A, deadline) < _bestof_obj(prob, best, deadline) - 1e-9:
        return A
    return best


# --------------------------------------------------------------------------- #
# solution assembly + top-level solve
# --------------------------------------------------------------------------- #
def _score_and_pack(prob, assign, poly_deadline=None):
    """Best-of(AABB, polygon) full objective AND the exact packed bays used for that
    score, in a single pass. The polygon pack is the same work build_solution needs
    to emit the solution, so scoring a final candidate and building the winner from
    it share that cost (no double packing). Returns (total, packed) where
    packed = [(bay, placed, exits), ...]. Mirrors build_solution's per-bay logic:

    Always pack AABB; ALSO pack with polygon escalation when there's time, and keep
    whichever gives lower tardiness. Polygon packing is usually better (recovers
    packing-driven tardiness) but is greedy -- placing one block earlier can push
    others out -- so it can occasionally be WORSE than AABB; best-of guarantees we
    never lose to the AABB packing. SOLVER_NOPOLY=1 forces AABB-only (ablation)."""
    w = prob["weights"]
    m = len(prob["bays"])
    mask_on = bool(os.environ.get("SOLVER_MASK")) and _HAS_SHAPELY
    mask_R = int(os.environ.get("SOLVER_MASK_R", "8"))
    obj1 = 0.0
    packed = []
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        placed = solve_bay(prob, j, ids, poly=False)
        T_best, exits = extract_tardiness(prob, j, placed)
        # SOLVER_MASK: best-of(AABB, supercover-mask) escalation in place of polygon.
        # Mask is conservative vs polygon (mask-disjoint => polygon-disjoint), so the
        # build stays feasible; best-of guarantees it never loses to AABB on a bay.
        if mask_on and (poly_deadline is None or time.time() < poly_deadline):
            placed_m = solve_bay(prob, j, ids, mask=True, mask_R=mask_R, deadline=poly_deadline)
            T_m, exits_m = extract_tardiness(prob, j, placed_m)
            if T_m < T_best:
                placed, exits, T_best = placed_m, exits_m, T_m
        else:
            use_poly = ((poly_deadline is None or time.time() < poly_deadline)
                        and not os.environ.get("SOLVER_NOPOLY"))
            if use_poly:
                placed_p = solve_bay(prob, j, ids, poly=True, deadline=poly_deadline)
                T_poly, exits_p = extract_tardiness(prob, j, placed_p)
                if T_poly < T_best:
                    placed, exits, T_best = placed_p, exits_p, T_poly
        packed.append((j, placed, exits))
        obj1 += T_best
    obj2, obj3 = obj23(prob, assign)
    total = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    return total, packed


def _solution_from_packed(packed):
    """Assemble the submission operations from already-packed bays."""
    ops = {}
    for j, placed, exits in packed:
        for p in placed:
            en, ex = int(p["entry"]), int(exits[p["id"]])
            ops.setdefault(str(ex), []).append({"type": "EXIT", "block_id": p["id"], "bay_id": j})
            ops.setdefault(str(en), []).append({"type": "ENTRY", "block_id": p["id"], "bay_id": j,
                                                "x": int(p["x"]), "y": int(p["y"]), "orient_idx": p["o"]})
    for k in ops:
        ops[k].sort(key=lambda d: 0 if d["type"] == "EXIT" else 1)
    # time keys in sorted order: check_feasibility reconstructs in insertion order,
    # and an EXIT key before its ENTRY key would lose the exit_time.
    ops = {k: ops[k] for k in sorted(ops, key=int)}
    return {"operations": ops}


def build_solution(prob, assign, poly_deadline=None):
    _, packed = _score_and_pack(prob, assign, poly_deadline=poly_deadline)
    return _solution_from_packed(packed)


def framework_solve(prob, timelimit):
    """Time-managed solve. Reserves a build margin, splits the rest between the
    two-phase search and the focused search (best-of, shared cache), and always
    returns a feasible solution."""
    global _EVALS, _EVAL_LIMIT, _TRACE_ON, _TRACE_T0, _TRACE_BEST
    _EVALS = 0
    _POOL.clear()
    # These caches are keyed by id(block_data); a later instance's block dict can
    # reuse a freed address, so a stale entry would corrupt packing if the module
    # is reused across problems (multi-instance harness/benchmark). Production runs
    # one problem per process, but clearing per solve makes reuse safe and is cheap.
    _LOCAL_FP.clear()
    _LOCAL_BOX.clear()
    _ORIENT_BBOX.clear()
    _LOCAL_MASK.clear()
    t0 = time.time()
    _TRACE_ON = bool(os.environ.get("SOLVER_TRACE"))
    if _TRACE_ON:
        _TRACE.clear()
        _TRACE_T0 = t0
        _TRACE_BEST = float("inf")
    cache = {}
    safety = max(2.0, timelimit * 0.04)
    recomb_on = _HAS_ORTOOLS and not os.environ.get("SOLVER_NORECOMB")

    # EVALUATION mode (reproducible, deterministic): stop the searches by candidate-
    # evaluation count, run the full polygon build, and let the harness report wall
    # time. Default (submission) mode is wall-clock time-managed.
    max_evals = os.environ.get("SOLVER_MAX_EVALS")
    _EVAL_LIMIT = int(max_evals) if max_evals else None

    # best heuristic as a guaranteed feasible floor; also detect whether tardiness
    # is even in play (min obj1 over seeds).
    best_seed, bt, min_o1 = None, float("inf"), float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob)
        tot, perbay = total_obj(prob, a, cache)
        min_o1 = min(min_o1, sum(perbay.values()))
        if tot < bt:
            bt, best_seed = tot, a
    best, best_tot = best_seed, bt

    recomb_deadline = None
    if _EVAL_LIMIT is not None:
        # eval-count thresholds (cumulative): improved 40%, local 70%, ILS 100%.
        imp_dl, bas_dl, ils_dl = _EVAL_LIMIT * 0.4, _EVAL_LIMIT * 0.7, _EVAL_LIMIT
        poly_deadline = None    # full deterministic polygon build
        if recomb_on:
            recomb_deadline = None   # set just before the call (wall-clock cap)
    else:
        # The exact polygon final-build only recovers PACKING-driven tardiness, so
        # reserve time for it only when tardiness is in play; preference-only
        # instances (a seed already reaches obj1=0) give the budget to search.
        # Reserve fractions/floors are env-tunable for budget-allocation A/B (default
        # unchanged). Calibrated pre-speedup; the find_slot work made build/recombine
        # ~4x faster, so these may now over-reserve and starve the search. See
        # docs/experiment_log.md.
        poly_frac = float(os.environ.get("SOLVER_POLY_RESERVE", "0.30"))
        recomb_frac = float(os.environ.get("SOLVER_RECOMB_RESERVE", "0.18"))
        poly_floor = float(os.environ.get("SOLVER_POLY_FLOOR", "6.0"))
        recomb_floor = float(os.environ.get("SOLVER_RECOMB_FLOOR", "5.0"))
        poly_build_reserve = (max(poly_floor, timelimit * poly_frac) if min_o1 > 1e-9
                              else max(1.0, timelimit * 0.04))
        # the recombination (MIP + best-of guard) needs ~2 builds' worth of time
        recombine_reserve = max(recomb_floor, timelimit * recomb_frac) if recomb_on else 0.0
        if os.environ.get("SOLVER_ADAPTIVE_RESERVE"):
            # Adaptive reserve (env-gated): the fixed POLY fraction above was calibrated
            # pre-speedup and over-reserves on fast builds -- it starves the ILS and
            # leaves a large idle tail (wall << timelimit at minute+ budgets). Size the
            # polygon-build reserve from the MEASURED cost of one best-of build of the
            # seed instead, capped by the fixed fraction (so never larger than the current
            # default -> downside bounded) and floored. Self-tunes to instance size: a
            # fast/small build frees the tail to the search, a slow/large build keeps
            # enough reserve to protect the final build. The probe build is wall-clock
            # work accounted for via `now` below.
            #
            # The RECOMBINE reserve is intentionally left at its fixed fraction: recombine
            # cost is driven by set-partitioning MIP hardness, not by build cost, so sizing
            # it from build_cost starved it and regressed recombine-dependent instances
            # (prob_11 +23.7% under build-cost recombine sizing -> -14.9% once kept fixed).
            poly_margin = float(os.environ.get("SOLVER_ADAPT_POLY_MARGIN", "2.0"))
            _tb = time.time()
            _score_and_pack(prob, best_seed, poly_deadline=t0 + timelimit - safety)
            build_cost = time.time() - _tb
            if min_o1 > 1e-9:
                poly_build_reserve = min(poly_build_reserve,
                                         max(poly_floor, build_cost * poly_margin))
            now = time.time()
            search_total = max(0.0, (timelimit - safety) - (now - t0)
                               - poly_build_reserve - recombine_reserve)
            imp_dl = now + search_total * 0.5
            bas_dl = ils_dl = now + search_total
            recomb_deadline = now + search_total + recombine_reserve
            poly_deadline = t0 + timelimit - safety
        else:
            search_total = max(0.0, timelimit - poly_build_reserve - recombine_reserve - safety)
            imp_dl = t0 + search_total * 0.5
            bas_dl = ils_dl = t0 + search_total
            recomb_deadline = t0 + search_total + recombine_reserve
            poly_deadline = t0 + timelimit - safety

    base_incumbent = None
    try:
        if os.environ.get("SOLVER_UNIFIED_ILS"):
            # Unified ILS (env-gated): one loop replaces the improved->local->ILS
            # pipeline. First establish an incumbent (improved_search's own-seed climb
            # plus a local_search from the heuristic seed) within the opening
            # INIT_FRAC of the search window, snapshot it as the trusted base, then
            # spend the WHOLE remaining budget perturbing the global best and
            # re-climbing the kicked point with best-of(_climb [improved's Z1+Z2+Z3
            # strategy from a start], local_search [Z1-only from a start]). This fixes
            # the legacy split where ils_dl == end-of-search left ILS ~0s of budget
            # once local_search consumed the window. Each climb runs to
            # convergence-or-deadline, so kicks get enough time to settle.
            init_frac = float(os.environ.get("SOLVER_UNIFIED_INIT_FRAC", "0.4"))
            # _now() is unit-agnostic (wall seconds, or eval-count in SOLVER_MAX_EVALS
            # mode) and matches the unit of ils_dl, so the init split is correct in both.
            search_start = _now()
            loop_dl = ils_dl
            init_dl = search_start + max(0.0, loop_dl - search_start) * init_frac
            asg_imp, t_imp = improved_search(prob, cache, deadline=init_dl)
            if t_imp < best_tot:
                best, best_tot = asg_imp, t_imp
            asg_bas, t_bas = local_search(prob, best_seed, cache, deadline=init_dl)
            if t_bas < best_tot:
                best, best_tot = asg_bas, t_bas
            base_incumbent = dict(best)
            rng = random.Random(0)
            # Per-kick inner-climb stop: by default deadline-driven (loop_dl), but
            # SOLVER_UNIFIED_PATIENCE switches each climb to a timing-independent
            # "stop after K consecutive non-improving evals" rule (loop_dl stays as the
            # hard safety cap). Decouples per-kick effort from wall time -> less wall
            # variance + more kicks when climbs converge early.
            _pat = os.environ.get("SOLVER_UNIFIED_PATIENCE")
            patience = int(_pat) if _pat else None
            while _within(loop_dl):
                kicked = _perturb(prob, best, cache, rng)
                c1, t1 = _climb(prob, kicked, cache, loop_dl, patience=patience)
                c2, t2 = local_search(prob, kicked, cache, loop_dl, patience=patience)
                cc, ct = (c1, t1) if t1 <= t2 else (c2, t2)
                if ct < best_tot - 1e-9:
                    best, best_tot = cc, ct
        else:
            asg_imp, t_imp = improved_search(prob, cache, deadline=imp_dl)
            if t_imp < best_tot:
                best, best_tot = asg_imp, t_imp
            asg_bas, t_bas = local_search(prob, best_seed, cache, deadline=bas_dl)
            if t_bas < best_tot:
                best, best_tot = asg_bas, t_bas
            # Snapshot the pre-ILS incumbent. The proxy-driven tail below (ILS, and the
            # recombination guarded only against ITS input) optimises the AABB proxy
            # (total_obj), but the submission is built on the best-of(AABB, polygon)
            # objective. A proxy gain can be a true-objective regression, so this trusted
            # incumbent anchors the final true-objective guard (after the searches).
            base_incumbent = dict(best)
            # iterated local search on whatever budget the main search left unused
            # (SOLVER_NOILS=1 disables it -- ablation baseline)
            rng = random.Random(0)
            do_ils = not os.environ.get("SOLVER_NOILS")
            # H2 (search -> recombine -> search loop) was tested and rejected: splitting the
            # ILS budget to recombine mid-search commits the assignment to a basin built from
            # a thin pool, then burns the rest re-searching there. Deterministic E=2000 A/B
            # vs the single final recombine: prob_13 +9.9%, prob_17 +57.7%, prob_5 0% -- worse
            # everywhere, no winning instance. One uninterrupted ILS + one final recombine of
            # the rich pool is strictly better. See docs/experiment_log.md.
            if do_ils:
                best, best_tot = _ils(prob, best, best_tot, cache, deadline=ils_dl, rng=rng)
        # Z2-aware set-partitioning recombination of the cached pieces (guarded;
        # adopted only if the full best-of objective improves). SOLVER_NORECOMB=1
        # disables it. Shared by both search branches.
        if recomb_on:
            rdl = recomb_deadline if recomb_deadline is not None else (time.time() + 30.0)
            best = _recombine(prob, best, deadline=rdl)
    except Exception:
        pass  # keep the best feasible assignment found so far

    # Final true-objective guard (Guarded methodology). The proxy-driven tail (ILS +
    # recombination) may have moved `best` off `base_incumbent` on the AABB proxy
    # only. Score BOTH on the real best-of objective with _score_and_pack and submit
    # whichever is truly better, reusing the winner's packing so it is emitted without
    # re-packing. `best` is scored first so the candidate we would have shipped keeps
    # the full polygon budget; base_incumbent is then scored under the same deadline,
    # so the guard overrides only when base_incumbent is genuinely better -- never a
    # regression vs the previous behaviour. Skipped when best == base_incumbent (the
    # common case: no proxy move stuck), keeping that path bit-identical.
    if base_incumbent is not None and best != base_incumbent:
        try:
            cand_obj, cand_packed = _score_and_pack(prob, best, poly_deadline=poly_deadline)
            base_obj, base_packed = _score_and_pack(prob, base_incumbent, poly_deadline=poly_deadline)
            if base_obj < cand_obj - 1e-9:
                return _solution_from_packed(base_packed)
            return _solution_from_packed(cand_packed)
        except Exception:
            pass  # any failure: fall through to the standard build of `best`

    return build_solution(prob, best, poly_deadline=poly_deadline)
