"""Packing and geometry kernels for the OGC 2026 solver."""
from __future__ import annotations
import math
import os
import time
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
    from numba import njit as _njit
    _HAS_NUMBA = True
except Exception:                       # pragma: no cover
    _HAS_NUMBA = False


def _env_flag(name, default=False):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return str(v).lower() not in ("0", "false", "no", "off")


# Optional numba-jitted AABB candidate scan (env-gated by SOLVER_NUMBA; OFF by default so
# the submission/default path stays bit-identical). The inner overlap loop of find_slot is
# ~97% of search time; jitting it yields many more candidate evaluations per wall-second.
# It returns the SAME first bottom-left free (x, y) as the pure-Python loop, so it is
# behaviour-invariant -- validate with SOLVER_MAX_EVALS (identical objective at the same
# eval count, only lower wall). Falls back to pure Python if numba is unavailable.
_NUMBA_ON = _HAS_NUMBA and _env_flag("SOLVER_NUMBA")

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
def find_slot(bay, present_objs, overlap_objs, bd, bid, W, H, step, ov_boxes=None):
    """Earliest bottom-left feasible (x, y, o): in-bounds, crane-entry OK, and
    full-footprint (AABB) disjoint from all temporally-overlapping blocks
    (=> no cross-layer overlap => never crane-trapped)."""
    if ov_boxes is None:
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
                if not check_entry(bay, present_objs, cand, fast=True):
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
                if check_entry(bay, present_objs, cand, fast=True):  # boundary only now
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
_MASK_SEARCH = _HAS_SHAPELY and _env_flag("SOLVER_MASK_SEARCH")
_MASK_R_SEARCH = int(os.environ.get("SOLVER_MASK_SEARCH_R", "8"))
# SOLVER_MASK_PREPARE: shapely.prepare the buffer before the per-cell point-membership
# test in _local_mask -> ~4.5x faster mask build (the point test is ~92% of build cost),
# BIT-IDENTICAL mask (0 mismatch validated) so feasibility is untouched. Gated OFF by
# default: at a fixed eval count the objective is unchanged, but in WALL mode the freed
# build time lets the search run further -- which is net -4.8% on train BUT exposes the
# search's non-monotonicity (proxy-drift: prob_12 +38.8%, prob_15 +35.6%). Keep off until
# the snapshot true-scoring guard makes "more search never hurts"; then flip it on.
_MASK_PREPARE = _HAS_SHAPELY and _env_flag("SOLVER_MASK_PREPARE")


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
    inside = _shapely.intersects_xy(buf, CX.ravel(), CY.ravel())
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
                if check_entry(bay, present_objs, cand, fast=True):
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
            if not check_entry(bay, present_objs, cand, fast=True):
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
                if check_entry(bay, present_objs, cand, fast=True):
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
            present = [p["blk"] for p in placed if p["entry"] <= t < p["exit"]]
            overlap = [p for p in placed if p["entry"] < t + P and t < p["exit"]]
            overlap_objs = [p["blk"] for p in overlap]
            ov_boxes = [p["bb"] for p in overlap]
            slot = find_slot(bay, present, overlap_objs, bd, i, W, H, step, ov_boxes)
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
                present = [p["blk"] for p in placed if p["entry"] <= tt < p["exit"]]
                overlap = [p for p in placed if p["entry"] < tt + P and tt < p["exit"]]
                overlap_objs = [p["blk"] for p in overlap]
                ov_boxes = [p["bb"] for p in overlap]
                slot = find_slot(bay, present, overlap_objs, bd, i, W, H, step, ov_boxes)
                if slot:
                    chosen = (tt, slot[0], slot[1], slot[2])
                    break
            if chosen is None:
                o_fit = next((o for o in range(len(bd["shape"]))
                              if math.ceil(max(0.0, -orient_bbox(bd, o)[0])) + orient_bbox(bd, o)[2] <= W
                              and math.ceil(max(0.0, -orient_bbox(bd, o)[1])) + orient_bbox(bd, o)[3] <= H), 0)
                mnx, mny, _, _ = orient_bbox(bd, o_fit)
                chosen = (t, math.ceil(max(0.0, -mnx)), math.ceil(max(0.0, -mny)), o_fit)
        blk_o = Block(i, bd, chosen[1], chosen[2], chosen[3])
        rec = {"id": i, "x": chosen[1], "y": chosen[2], "o": chosen[3],
               "entry": chosen[0], "exit": chosen[0] + P,
               "blk": blk_o, "bb": blk_o.bounding_rect()}
        if use_mask:  # cache bbox + local mask + world grid offsets for later mask checks
            lm = _local_mask(bd, chosen[3], mask_R)
            rec["mask"] = lm
            rec["mix0"] = lm.ix0 + chosen[1] * mask_R
            rec["miy0"] = lm.iy0 + chosen[2] * mask_R
        elif use_poly:  # cache footprint + bbox so later poly checks never rebuild
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
                    if not check_exit(bay, [blk[k] for k in present], blk[i], fast=True):
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


def clear_packing_caches():
    """Clear id()-keyed per-instance geometry caches before each solve."""
    _LOCAL_FP.clear()
    _LOCAL_BOX.clear()
    _ORIENT_BBOX.clear()
    _LOCAL_MASK.clear()
