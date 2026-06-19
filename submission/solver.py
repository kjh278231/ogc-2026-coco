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
    import gurobipy as _gp
    from gurobipy import GRB as _GRB
    _HAS_GUROBI = True
except Exception:                       # pragma: no cover
    _HAS_GUROBI = False

# One silent Gurobi environment per process, started lazily so the WLS license is
# checked out exactly once and reused across every MIP solve (creating an env per
# model would re-checkout the license each call). OutputFlag=0 also suppresses the
# license banner and per-solve logs.
_GRB_ENV = None


def _grb_env():
    global _GRB_ENV
    if _GRB_ENV is None:
        _GRB_ENV = _gp.Env(empty=True)
        _GRB_ENV.setParam("OutputFlag", 0)
        _GRB_ENV.start()
    return _GRB_ENV

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

    @_njit(cache=True)
    def _scan_mask_orient(a_rows, a_h, a_ww, a_ix0, a_iy0,
                          x_start, x_end, y_start, y_end, step,
                          lbx0, lby0, lbx1, lby1, R,
                          pm_rows, pm_h, pm_ww, pm_ix0, pm_iy0, pm_box, n, ry, rx):
        """Scan one orientation's (x, y) grid for the FIRST supercover-mask-disjoint
        placement at/after the resume point (ry, rx), AABB-prefiltered against the n
        present masks. Returns (x, y) or (-1, -1). This moves find_slot_mask's inner
        position loop + the billion masks_overlap calls into one jit (no per-call
        Python<->numba boundary). check_entry stays in Python (resumes via ry/rx)."""
        y = ry
        while y <= y_end:
            cy0 = lby0 + y
            cy1 = lby1 + y
            cand_ay = a_iy0 + y * R
            x = rx if y == ry else x_start
            while x <= x_end:
                cx0 = lbx0 + x
                cx1 = lbx1 + x
                cand_ax = a_ix0 + x * R
                bad = False
                for p in range(n):
                    if not (cx0 < pm_box[p, 2] and pm_box[p, 0] < cx1
                            and cy0 < pm_box[p, 3] and pm_box[p, 1] < cy1):
                        continue  # AABB-disjoint => footprints disjoint => safe
                    if _masks_overlap_u64(a_rows, cand_ay, a_h, a_ww,
                                          pm_rows[p], pm_iy0[p], pm_h[p], pm_ww[p],
                                          cand_ax - pm_ix0[p]):
                        bad = True
                        break
                if not bad:
                    return x, y
                x += step
            y += step
        return -1, -1

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


def _find_slot_mask_jit(bay, present_objs, ov_boxmasks, bd, bid, W, H, step, R):
    """Numba-scan find_slot_mask: marshal the present masks into padded uint64 arrays
    ONCE, then scan each orientation entirely inside _scan_mask_orient (removes the
    per-position masks_overlap Python<->numba boundary -- the profiled #1 hot path).
    Behaviour-identical to the pure-Python find_slot_mask: same AABB pre-filter, same
    supercover overlap test, same first-fit order; check_entry stays in Python and
    resumes the scan after a crane-rejected slot."""
    n = len(ov_boxmasks)
    if n:
        max_h = max(1, max(bm[1].height_rows for bm in ov_boxmasks))
        max_w = max(1, max(bm[1].width_words for bm in ov_boxmasks))
    else:
        max_h = max_w = 1
    nn = max(1, n)
    pm_rows = _np.zeros((nn, max_h, max_w), dtype=_np.uint64)
    pm_h = _np.zeros(nn, dtype=_np.int64)
    pm_ww = _np.zeros(nn, dtype=_np.int64)
    pm_ix0 = _np.zeros(nn, dtype=_np.int64)
    pm_iy0 = _np.zeros(nn, dtype=_np.int64)
    pm_box = _np.zeros((nn, 4), dtype=_np.float64)
    for p, (ob_box, ob_mask, ob_mix0, ob_miy0) in enumerate(ov_boxmasks):
        h, wds = ob_mask.height_rows, ob_mask.width_words
        if h and wds:
            pm_rows[p, :h, :wds] = ob_mask.rows_u64
        pm_h[p] = h
        pm_ww[p] = wds
        pm_ix0[p] = ob_mix0
        pm_iy0[p] = ob_miy0
        pm_box[p, 0] = ob_box[0]
        pm_box[p, 1] = ob_box[1]
        pm_box[p, 2] = ob_box[2]
        pm_box[p, 3] = ob_box[3]
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        x_start = int(math.ceil(max(0.0, -mnx)))
        x_end = int(math.floor(W - mxx))
        y_start = int(math.ceil(max(0.0, -mny)))
        y_end = int(math.floor(H - mxy))
        if x_end < x_start or y_end < y_start:
            continue
        lbx0, lby0, lbx1, lby1 = _local_box(bd, o)
        cmask = _local_mask(bd, o, R)
        a_rows = cmask.rows_u64
        if a_rows.shape[1] == 0:                       # empty footprint -> numba-safe shape
            a_rows = _np.zeros((max(1, cmask.height_rows), 1), dtype=_np.uint64)
        ry, rx = y_start, x_start
        while ry <= y_end:
            x, y = _scan_mask_orient(
                a_rows, int(cmask.height_rows), int(cmask.width_words),
                int(cmask.ix0), int(cmask.iy0),
                x_start, x_end, y_start, y_end, int(step),
                float(lbx0), float(lby0), float(lbx1), float(lby1), int(R),
                pm_rows, pm_h, pm_ww, pm_ix0, pm_iy0, pm_box, n, ry, rx)
            if x < 0:
                break
            cand = Block(block_id=bid, block_data=bd, x=x, y=y, orient_idx=o)
            if not check_entry(bay, present_objs, cand):
                return (x, y, o)
            rx, ry = x + step, y                       # crane-rejected -> resume after (x, y)
            if rx > x_end:
                rx, ry = x_start, y + step
    return None


def find_slot_mask(bay, present_objs, ov_boxmasks, bd, bid, W, H, step, R):
    """Like find_slot_poly but disjointness is the SUPERCOVER bitmask instead of the
    exact polygon: more permissive than AABB (packs tighter), conservative vs polygon
    (mask-disjoint => polygon-disjoint, so feasibility holds), and far cheaper than
    shapely. `ov_boxmasks` = [(bbox, MaskProxy, world_ix0, world_iy0)] of the
    temporally-overlapping placed blocks; AABB is the cheap pre-filter (AABB-disjoint
    => footprints disjoint => safe, since footprint <= AABB)."""
    if _NUMBA_ON:
        return _find_slot_mask_jit(bay, present_objs, ov_boxmasks, bd, bid, W, H, step, R)
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
    """Full objective on the SAME per-bay model the final build uses (_score_and_pack):
    supercover-mask ONLY when SOLVER_MASK is on -- matching the search proxy exactly so
    `more search` can no longer drift the guard/build off what the search optimizes --
    else best-of(AABB, polygon). w1·ΣT + w2·Z2 + w3·Z3. Guards recombination adoption so
    it predicts the SHIPPED objective (Pareto-safe). Mask is ~15x cheaper than polygon."""
    w = prob["weights"]
    m = len(prob["bays"])
    mask_on = bool(os.environ.get("SOLVER_MASK")) and _HAS_SHAPELY
    mask_R = int(os.environ.get("SOLVER_MASK_R", "8"))
    o1 = 0.0
    for j in range(m):
        ids = [i for i, a in assign.items() if a == j]
        if not ids:
            continue
        if mask_on:
            # mask-only, identical to the search proxy and the final build (no AABB best-of)
            T, _ = extract_tardiness(prob, j, solve_bay(
                prob, j, ids, mask=True, mask_R=mask_R, deadline=deadline))
        else:
            Ta, _ = extract_tardiness(prob, j, solve_bay(prob, j, ids, poly=False))
            Talt, _ = extract_tardiness(prob, j, solve_bay(
                prob, j, ids, poly=True, deadline=deadline))
            T = min(Ta, Talt)
        o1 += T
    o2, o3 = obj23(prob, assign)
    return w["w1"] * o1 + w["w2"] * o2 + w["w3"] * o3


def _pool_prune_key(prob, T_index=2, ids_index=1):
    """Per-bay pool-prune sort key for a piece tuple c with c[0]=bay, c[ids_index]=ids,
    c[T_index]=tardiness. Keyed by the recombine COLUMN COST (w1*T + w3*pref_loss) by DEFAULT
    -- aligned with the (Z3-dominant) objective, so the prune keeps objective-relevant pieces
    (ties: wider coverage first), not merely
    low-tardiness ones. Validated: when the prune is tight, tardiness-only keeps low-T/bad-Z3
    pieces that won't tile, so recombine finds NO improvement, while cost-prune fires (prob_6
    -47%, prob_13 -25%; cost <= tardiness on every tested instance). SOLVER_POOL_PRUNE_COST=0
    restores the legacy tardiness key. When the pool is small enough that no prune happens
    (pool <= per_bay*m, e.g. the T=60 regime) both keys are bit-identical (no pieces dropped)."""
    if os.environ.get("SOLVER_POOL_PRUNE_COST", "1") == "0":
        return lambda c: (c[T_index], -len(c[ids_index]))
    w = prob["weights"]; blocks = prob["blocks"]
    w1, w3 = w["w1"], w["w3"]
    mx = [max(b["bay_preferences"]) for b in blocks]

    def key(c):
        j = c[0]; ids = c[ids_index]
        pl = 0
        for i in ids:
            pl += mx[i] - blocks[i]["bay_preferences"][j]
        return (w1 * c[T_index] + w3 * pl, -len(ids))
    return key


def _recombine(prob, best, deadline):
    """Z2-aware set-partitioning recombination of the cached (bay,set) pieces.
    Local search moves one block at a time and cannot recombine whole bay-pieces
    across solutions; the MIP can. column cost = w1·tardiness + w3·preference,
    global term = w2·Z2 (min-max, linearized). Hinted with the incumbent (so a
    time cut-off still yields >= incumbent on the MIP metric) and adopted only if
    the full best-of objective actually improves -- Pareto-safe."""
    if not _HAS_GUROBI:
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
    # Per-bay top-N prune (SOLVER_POOL_PER_BAY, default 1000; 0=off): bounds the MIP
    # model AND keeps it solvable within the 8s cap. The model-build and solve cost both
    # grow with the pool (one binary var + constraints per column) and are otherwise unbounded
    # as the search budget -- hence the pool -- grows: at long timelimits the uncapped MIP
    # could not be solved well in 8s, so recombine returned `best` unchanged and could not
    # rescue a poor incumbent (prob_20 E=10000: 1.87M uncapped vs 618k capped). Keep each
    # bay's lowest-tardiness pieces (ties: wider
    # coverage first) plus the incumbent's own pieces (so the hint stays valid and
    # `best` remains representable -> the guard can never adopt something worse). The
    # recombine benefit plateaus within a few thousand columns (see experiment board).
    _per_bay = int(os.environ.get("SOLVER_POOL_PER_BAY", "1000"))
    if _per_bay > 0 and len(cols) > _per_bay * m:
        inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
               for j in range(m) if any(best[i] == j for i in best)}
        # Prune key: by default keep each bay's lowest-TARDINESS pieces. The objective is
        # Z3-dominant, though, so tardiness-only keeps low-T pieces that may be bad on the
        # dominant w3*pref term and discards moderate-T/low-pref pieces the recombine needs
        # (this is what diluted the portfolio's cross-worker UNION -> recombine found no
        # improvement). SOLVER_POOL_PRUNE_COST switches the key to the piece's actual recombine
        # COLUMN COST (w1*T + w3*pref_loss), i.e. its real contribution to the objective.
        _key = _pool_prune_key(prob, T_index=2, ids_index=1)
        bins = [[] for _ in range(m)]
        for c in cols:
            bins[c[0]].append(c)
        cols = []
        for j in range(m):
            b = bins[j]
            b.sort(key=_key)
            keep = b[:_per_bay]
            kept = {(c[0], c[1]) for c in keep}
            keep.extend(c for c in b if (c[0], c[1]) in inc and (c[0], c[1]) not in kept)
            cols.extend(keep)
    model = _gp.Model(env=_grb_env())
    x = [model.addVar(vtype=_GRB.BINARY, name=f"c{k}") for k in range(len(cols))]
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
            model.dispose()
            return best
        model.addConstr(_gp.quicksum(x[k] for k in by_block[i]) == 1)
    for j in range(m):
        if by_bay[j]:
            model.addConstr(_gp.quicksum(x[k] for k in by_bay[j]) <= 1)
    obj = _gp.quicksum(cost[k] * x[k] for k in range(len(cols)))
    if m >= 2:
        areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
        cj = [round(avg / areas[j] * SCALE) for j in range(m)]
        sload = [_gp.quicksum(cj[j] * wl[k] * x[k] for k in by_bay[j]) for j in range(m)]
        M = model.addVar(lb=0, ub=10 ** 12, vtype=_GRB.INTEGER, name="M")  # = SCALE * Z2
        for a in range(m):
            for b in range(m):
                if a != b:
                    model.addConstr(M >= sload[a] - sload[b])
        obj = obj + w["w2"] * M
    model.setObjective(obj, _GRB.MINIMIZE)
    inc = {(j, tuple(sorted(i for i in best if best[i] == j)))
           for j in range(m) if any(best[i] == j for i in best)}
    # MIP start = the incumbent (kept representable by the prune above), so a time
    # cut-off still returns a solution >= the incumbent on the MIP metric.
    for k, (j, ids, T) in enumerate(cols):
        x[k].Start = 1 if (j, ids) in inc else 0
    # The recombine objective fully plateaus by ~8s of MIP (measured: the set-
    # partitioning improvement appears at a threshold then is flat -- see
    # docs/experiment_board.md); the guard is now mask-cheap, so give the solve the
    # whole reserve up to that 8s cap (env-tunable). Previously max_time scaled with
    # the reserve (=timelimit*0.18*0.5), so at long timelimits it wasted 15-20s of
    # solver time for zero gain and inflated the idle tail.
    _solve_cap = float(os.environ.get("SOLVER_RECOMB_SOLVE_S", "8"))
    model.Params.TimeLimit = min(_solve_cap, max(0.5, deadline - time.time()))
    # default 4 threads; a parallel portfolio sets SOLVER_CP_WORKERS=1 so N chains use
    # N*1 cores (no >4-thread over-subscription under the 4-core cpulimit).
    model.Params.Threads = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
    model.optimize()
    if model.SolCount == 0:
        model.dispose()
        return best
    A = {}
    for k, (j, ids, T) in enumerate(cols):
        if x[k].X > 0.5:
            for i in ids:
                A[i] = j
    model.dispose()
    if len(A) != n:
        return best
    # best-of full-objective guard (Pareto-safe): adopt only if A truly improves on
    # the SAME best-of model the build ships (_bestof_obj mirrors _score_and_pack).
    if _bestof_obj(prob, A, deadline) < _bestof_obj(prob, best, deadline) - 1e-9:
        return A
    return best


# --------------------------------------------------------------------------- #
# solution assembly + top-level solve
# --------------------------------------------------------------------------- #
def _score_and_pack(prob, assign, poly_deadline=None):
    """Full objective AND the exact packed bays used for that score, in a single pass.
    Returns (total, packed) where packed = [(bay, placed, exits), ...].

    SOLVER_MASK (default): mask-ONLY -- score the same supercover-mask packing the search
    optimizes, so the build/score never disagrees with the search (kills the proxy seam
    that let `more search` drift the true objective). solve_bay(mask=True) always returns
    a complete feasible packing (mask-disjoint => polygon-disjoint; degrades per-block to
    AABB once `poly_deadline` passes), so this is feasible and time-bounded in one pass.
    Otherwise: best-of(AABB, polygon) -- always pack AABB, also escalate to polygon when
    there's time, keep whichever tardiness is lower. SOLVER_NOPOLY=1 forces AABB-only."""
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
        if mask_on:
            # mask-only: same packing the search optimizes (no AABB best-of) -> no seam.
            placed = solve_bay(prob, j, ids, mask=True, mask_R=mask_R, deadline=poly_deadline)
            T_best, exits = extract_tardiness(prob, j, placed)
        else:
            placed = solve_bay(prob, j, ids, poly=False)
            T_best, exits = extract_tardiness(prob, j, placed)
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


def _mip_repair(prob, mip_tl, repair_deadline):
    """Assignment-MIP propose + local-search repair -- a guarded best-of candidate.
    Gurobi minimizes w2*Z2 + w3*Z3 over block->bay assignments (fits-constrained): a
    low-preference-loss assignment the single-trajectory ILS can't reach from a_pref.
    That assignment usually carries tardiness (Z1>0, catastrophic given the large w1),
    so local_search repairs Z1 to ~0 while keeping the low Z3. Returns the repaired
    assignment or None. Validated -16..-62% on high-Z3 instances (prob_11/13/20),
    never worse via the final best-of guard. Mirrors last year's winner's MIP-centric
    formulation (the Z2 min-max term acts as a soft capacity)."""
    if not _HAS_GUROBI:
        return None
    blocks = prob["blocks"]
    bays = prob["bays"]
    w = prob["weights"]
    n = len(blocks)
    m = len(bays)
    try:
        areas = [b["width"] * b["height"] for b in bays]
        avg = sum(areas) / m
        u = [avg / areas[j] for j in range(m)]
        md = _gp.Model(env=_grb_env())
        x = [[md.addVar(vtype=_GRB.BINARY) for j in range(m)] for i in range(n)]
        for i in range(n):
            md.addConstr(_gp.quicksum(x[i][j] for j in range(m)) == 1)
            for j in range(m):
                if not fits(blocks[i], bays[j]):
                    md.addConstr(x[i][j] == 0)
        load = [_gp.quicksum(blocks[i]["workload"] * x[i][j] for i in range(n)) for j in range(m)]
        Mv = md.addVar(lb=0)
        for a in range(m):
            for b in range(m):
                if a != b:
                    md.addConstr(Mv >= u[a] * load[a] - u[b] * load[b])
        pref = _gp.quicksum(
            (max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]) * x[i][j]
            for i in range(n) for j in range(m))
        md.setObjective(w["w2"] * Mv + w["w3"] * pref, _GRB.MINIMIZE)
        md.Params.TimeLimit = max(0.5, mip_tl)
        md.Params.Threads = int(os.environ.get("SOLVER_CP_WORKERS", "4"))
        md.optimize()
        if md.SolCount == 0:
            md.dispose()
            return None
        A = {i: next(j for j in range(m) if x[i][j].X > 0.5) for i in range(n)}
        md.dispose()
    except Exception:
        return None
    try:
        A2, _ = local_search(prob, A, {}, repair_deadline)   # repair Z1 -> ~0, keep low Z3
        return A2
    except Exception:
        return A


def framework_solve(prob, timelimit, _return_assignment=False):
    """Time-managed solve. Reserves a build margin, splits the rest between the
    two-phase search and the focused search (best-of, shared cache), and always
    returns a feasible solution.

    _return_assignment (parallel-portfolio worker mode, SOLVER_PORTFOLIO harness):
    run search + recombine but return (best_assign, base_incumbent_assign) instead of
    materializing -- the master true-scores candidates from all workers on one
    consistent _score_and_pack basis. In this mode no final-build time is reserved
    (the worker emits nothing), so that budget goes to the search. Default False keeps
    the single-process path byte-identical."""
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
    safety = max(2.0, timelimit * 0.06)
    recomb_on = _HAS_GUROBI and not os.environ.get("SOLVER_NORECOMB")

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
        recomb_cap = float(os.environ.get("SOLVER_RECOMB_CAP", "10.0"))
        poly_build_reserve = (max(poly_floor, timelimit * poly_frac) if min_o1 > 1e-9
                              else max(1.0, timelimit * 0.04))
        # Recombine reserve: ABSOLUTE-capped (default 10s), not a raw fraction. Cost is
        # MIP-hardness-bound (~8s CP-SAT plateau) + a now-mask-cheap guard/build (~1s),
        # so it does NOT scale with timelimit. The old fraction (timelimit*0.18) ballooned
        # to 54s at a 300s limit -- search stopped early to leave it, recombine used <1/3,
        # and the rest became idle tail. The cap fits the recombine on the largest pools
        # (measured ~8.8s total) while freeing the surplus back to search at long limits;
        # at the 60s default min(10.8,10)=10 is effectively unchanged. Floor protects short
        # limits. See docs/experiment_board.md.
        recombine_reserve = (min(recomb_cap, max(recomb_floor, timelimit * recomb_frac))
                             if recomb_on else 0.0)
        if _return_assignment:
            # portfolio worker: no final build here, so reserve nothing for it and give
            # that budget to the search (the master does the single final build).
            poly_build_reserve = 0.0
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

    # MIP-propose + ILS-repair candidate (SOLVER_MIP_REPAIR, wall mode). FRONT-LOADED: a Gurobi
    # assignment MIP (min w2*Z2 + w3*Z3) proposes a low-preference-loss basin the single ILS
    # trajectory can't reach from a_pref, then local_search repairs its tardiness; carried to
    # the final best-of guard so it can only ever help. Serial by necessity -- the WLS license
    # is SINGLE-USE (one Gurobi process at a time), so it cannot overlap the search's recombine
    # (a concurrent sidecar makes recombine silently no-op -> prob_40 +44%). Front-loaded (not
    # tail-placed) because the search deadlines below are t0-based, so this costs a bounded slice
    # AND -- unlike a post-recombine tail slice -- it (a) still fires on the biggest winners
    # (prob_20), which have no idle tail for a tail slice, and (b) preserves the full final-build
    # margin (a tail slice overran prob_40). Cost is ~8s of search -> small regressions on
    # search-bound low-Z3 instances (prob_37 +10.9%), but the high-Z3 wins (-23..-48%) dominate.
    mip_cand = None
    if (os.environ.get("SOLVER_MIP_REPAIR") not in (None, "", "0")
            and _EVAL_LIMIT is None and not _return_assignment and _HAS_GUROBI):
        _mb = min(float(os.environ.get("SOLVER_MIP_REPAIR_CAP", "8")), timelimit * 0.15)
        mip_cand = _mip_repair(prob, _mb * 0.5, time.time() + _mb)

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
            rng = random.Random(int(os.environ.get("SOLVER_SEED", "0")))
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
            rng = random.Random(int(os.environ.get("SOLVER_SEED", "0")))
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
        # Opportunistic idle reclaim (SOLVER_IDLE_ILS, wall mode only). After recombine,
        # spend any leftover wall on MORE guarded ILS from the current best. The init/ILS
        # deadlines are UNCHANGED, so this only EXTENDS the same trajectory (best is a
        # running min -> monotonic, can't regress) -- unlike shrinking the reserve, which
        # moves the init deadline and reshapes the trajectory (drift). Targets the dominant
        # Z3/Z2 assignment cost. idle_dl leaves a build margin for the final build.
        if (os.environ.get("SOLVER_IDLE_ILS") not in (None, "", "0") and _EVAL_LIMIT is None
                and not _return_assignment):
            # Size the idle window from the ACTUAL build cost -- one throwaway build of the
            # current best (mask-only, ~1-3s) -- not the inflated pre-Gurobi a_pref reserve.
            # The margin must cover the final guard's builds (best, then the losing
            # base_incumbent) UNtruncated, else truncation -> AABB falls back -> adds Z1 on
            # build-sensitive instances and breaks monotonicity (observed prob_11 +9.9%).
            _t = time.time()
            _score_and_pack(prob, best, poly_deadline=poly_deadline)
            _build = time.time() - _t
            idle_dl = poly_deadline - max(0.5, _build
                                          * float(os.environ.get("SOLVER_IDLE_BUILD_MARGIN", "2.5")))
            _rng2 = random.Random(int(os.environ.get("SOLVER_SEED", "0")) + 7)
            while time.time() < idle_dl:
                kicked = _perturb(prob, best, cache, _rng2)
                c1, t1 = _climb(prob, kicked, cache, idle_dl)
                c2, t2 = local_search(prob, kicked, cache, idle_dl)
                cc, ct = (c1, t1) if t1 <= t2 else (c2, t2)
                if ct < best_tot - 1e-9:
                    best, best_tot = cc, ct
    except Exception:
        pass  # keep the best feasible assignment found so far

    # Portfolio worker mode: return the search result (post-recombine best + the trusted
    # base incumbent) for the master to true-score against other workers. No build here.
    if _return_assignment:
        return best, base_incumbent

    # Final true-objective guard (Guarded methodology). The proxy-driven tail (ILS +
    # recombination) may have moved `best` off `base_incumbent` on the AABB proxy
    # only. Score BOTH on the real best-of objective with _score_and_pack and submit
    # whichever is truly better, reusing the winner's packing so it is emitted without
    # re-packing. `best` is scored first so the candidate we would have shipped keeps
    # the full polygon budget; base_incumbent is then scored under the same deadline,
    # so the guard overrides only when base_incumbent is genuinely better -- never a
    # regression vs the previous behaviour. Skipped when best == base_incumbent (the
    # common case: no proxy move stuck), keeping that path bit-identical.
    # Best-of over {best, base_incumbent, mip_cand} on the true objective. `best` is scored
    # FIRST (keeps the full polygon budget) and wins ties, so adding candidates can only
    # improve the emitted solution -- never a regression. mip_cand is the MIP-repair candidate
    # (None unless SOLVER_MIP_REPAIR in wall mode). When only `best` remains this is the
    # original bit-identical build_solution(best) fast path.
    cands = [best]
    if base_incumbent is not None and base_incumbent != best:
        cands.append(base_incumbent)
    if mip_cand is not None and mip_cand != best and mip_cand not in cands:
        cands.append(mip_cand)
    if len(cands) > 1:
        try:
            win_obj, win_packed = _score_and_pack(prob, cands[0], poly_deadline=poly_deadline)
            for c in cands[1:]:
                o, pk = _score_and_pack(prob, c, poly_deadline=poly_deadline)
                if o < win_obj - 1e-9:
                    win_obj, win_packed = o, pk
            return _solution_from_packed(win_packed)
        except Exception:
            pass  # any failure: fall through to the standard build of `best`

    return build_solution(prob, best, poly_deadline=poly_deadline)
