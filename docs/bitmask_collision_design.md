# Bitmask Collision Design

## 1. Goal

Current packing uses two geometric collision models:

- **AABB-disjoint:** fast and conservative, but often over-rejects placements because a
  single bounding box contains much empty area.
- **Polygon-disjoint:** much tighter, but expensive because it depends on Shapely geometry
  construction, translation, and intersection area checks.

The goal of the bitmask design is to introduce a middle representation that is:

- **safer than exactness shortcuts:** never accepts a placement that true polygon collision
  would reject.
- **tighter than AABB:** recovers placements where AABBs overlap but real footprints do not.
- **close to AABB speed:** uses cached row bitsets and integer operations in the candidate
  scan.
- **usable as a unified search/build proxy:** reduces drift between assignment scoring and
  final polygon escalation.

The design is deliberately conservative: the bitmask is a **supercover** of the true
footprint. Therefore, if two masks do not overlap, the true polygons also do not overlap.

```text
mask_overlap == false  =>  true_polygon_overlap == false
mask_overlap == true   =>  true_polygon_overlap may be true or false
```

This direction preserves feasibility. The cost is possible over-rejection, which is tuned
by raster resolution.

## 2. Literature Position

This design sits between two research lines:

1. **Tighter bounding volumes.** AABB is cheap but loose; OBB, k-DOP, split bounding boxes,
   and BVHs improve tightness while keeping overlap tests simple. Kacerik and Bittner
   (2024) discuss k-DOPs as tighter bounding volumes than AABBs and report traversal
   benefits in BVH workloads.
2. **Raster / semi-discrete nesting representations.** Irregular 2-D nesting literature
   commonly uses raster or bitmap representations for fast feasibility checks. Bennell and
   Oliveira (2008) survey nesting methods and discuss raster representations as a practical
   alternative to exact geometric operations. Chehrazad et al. (2021) use a semi-discrete
   representation that expands shapes to prevent overlap in a placement-building workflow.

Relevant sources:

- Kacerik, M. and Bittner, J. (2024), k-DOP bounding-volume hierarchy work:
  <https://dcgi.fel.cvut.cz/wp-content/wpallimport-dist/publications/pdf/publications-2024-kacerik-hpg-kdop-paper.pdf>
- Bennell, J. A. and Oliveira, J. F. (2008), irregular shape packing / nesting survey:
  <https://gent.cs.kuleuven.be/vakgroepit/sites/gent.cs.kuleuven.be/files/nesting_problems_tutorialEJOR-184-2008.pdf>
- Chehrazad, S. et al. (2021), semi-discrete shape packing:
  <https://arxiv.org/pdf/2103.08739>

## 3. Data Observations

Measured on the 20 `train/` instances plus generated benchmark/example cases:

- Bay dimensions: width `32..179`, height `15..29`.
- Orientation bbox median size: about `11.6 x 11.4`.
- Footprint area / AABB area median: about `0.51`.
- Footprint area / AABB area 10th percentile: about `0.36`.
- Layers per orientation: usually `1..2`, max observed `4`.
- Vertices per orientation: median about `14`, 90th percentile about `18`, max `40`.

Interpretation: AABB leaves a lot of unused area inside the proxy. A tighter proxy can
recover meaningful placements, especially in dense geometry cases.

## 4. Resolution Choice

Let `R` be cells per bay unit. Cell size is `1 / R`.

Recommended default:

```text
R = 8
cell size = 0.125
```

Rationale:

| R | Cell size | Sample median mask/poly area | Role |
|---|---:|---:|---|
| 4 | 0.25 | about `1.10x` | Fast exploratory mode |
| 8 | 0.125 | about `1.05x` | Default balance |
| 16 | 0.0625 | about `1.025x` | High-precision final experiment |

Precompute cost on a hard training instance was acceptable at `R=8` and noticeably heavier
at `R=16`. Raw bit storage is small enough for all three, but row-shifting and candidate
scan cost scale with mask width/height, so `R=8` is the starting point.

Use environment gates:

```text
SOLVER_MASK=1
SOLVER_MASK_R=8
```

Keep the default solver path unchanged until validation is complete.

## 5. Supercover Rasterization

The unsafe rasterization is:

```text
occupied(cell) = cell_center inside polygon
```

This can miss thin overlaps and create infeasible placements. Do not use it for feasibility.

The safe rasterization is:

```text
d = sqrt(2) / (2R) + eps
occupied(cell) = cell_center inside footprint.buffer(d)
```

Reason:

- Any point in a square cell is at distance at most `sqrt(2) / (2R)` from the cell center.
- If the true polygon touches a cell, then at least one polygon point lies within that
  distance from the cell center.
- Therefore, checking the center against the polygon expanded by `d` marks every touched
  cell as occupied.

This creates a supercover of the true footprint.

Use a small epsilon:

```text
eps = 1e-9
```

The resulting mask is slightly more conservative than polygon, but cannot admit a false
negative collision if all later overlap checks use the same grid frame and integer shifts.

## 6. Footprint Scope

Use the **union of all layers** for each block orientation, matching the current
`find_slot_poly` footprint behavior.

Do not start with per-layer masks.

Reason:

- The current solver intentionally enforces full-footprint disjointness for temporally
  overlapping blocks.
- That invariant makes crane extraction safe by construction.
- Per-layer masks may improve packing but weaken this invariant and reopen crane-trap risk.

Thus each `(block_data, orient)` gets one local mask:

```text
local_footprint = unary_union(all valid layer polygons)
local_mask = supercover(local_footprint, R)
```

## 7. Coordinate Model

Each local mask stores:

```text
MaskProxy:
    R: int
    ix0: int   # local grid x offset, floor(min_x * R - margin)
    iy0: int   # local grid y offset
    width_bits: int
    height_rows: int
    rows: tuple[int, ...]  # row bitsets, low bit = local ix0
```

For a block placed at integer `(x, y)`, the mask grid offset is:

```text
world_ix0 = ix0 + x * R
world_iy0 = iy0 + y * R
```

Because current placement search uses integer coordinates with `step=2`, `x * R` and
`y * R` are exact integer shifts for integer `R`.

## 8. Overlap Test

Use AABB first, then mask:

```text
if AABB(candidate, other) are disjoint:
    safe
else:
    reject iff masks overlap
```

The row-bitset overlap test:

```text
def masks_overlap(a, ax, ay, b, bx, by):
    # ax, ay, bx, by are world grid offsets.
    y0 = max(ay, by)
    y1 = min(ay + a.height_rows, by + b.height_rows)
    if y0 >= y1:
        return False

    dx = ax - bx
    for gy in range(y0, y1):
        ar = a.rows[gy - ay]
        br = b.rows[gy - by]
        if dx >= 0:
            if ar & (br >> dx):
                return True
        else:
            if (ar >> -dx) & br:
                return True
    return False
```

Implementation can also align both rows to the same world origin by shifting the smaller
offset. Since bay heights are small and masks are sparse enough, a pure-Python integer
bitset version should be tested first. If it becomes hot, move the scan to numba or use
packed `uint64` arrays.

## 9. Packer Integration

Add a new mask-based slot finder:

```text
find_slot_mask(bay, present_objs, overlap_maskobjs, bd, bid, W, H, step)
```

It mirrors `find_slot_poly`:

1. Iterate orientations.
2. Compute integer feasible `x/y` range from exact orientation bbox, same as current code.
3. For each row, prefilter overlapping placed blocks by AABB y-range.
4. For each candidate x:
   - Use AABB x/y reject first.
   - For AABB-overlapping objects, run mask overlap.
   - If no mask overlap, build `Block` only for `check_entry`.
5. Return the first row-major feasible slot.

Placed records should cache:

```text
rec["bb"] = block bounding rect
rec["mask"] = local mask proxy
rec["mix0"] = local ix0 + x * R
rec["miy0"] = local iy0 + y * R
```

This avoids rebuilding masks and avoids translating Shapely polygons during the scan.

## 10. Build Policy

Do not replace everything at once. Add a gated policy:

```text
mask off:
    current AABB + optional polygon escalation

mask on:
    pack AABB
    pack mask
    choose best tardiness per bay
    optionally run polygon final validation
```

Use **best-of(AABB, mask)** initially.

Reason: a more permissive greedy packer can occasionally place an early block in a way that
hurts later blocks. This already happened with polygon escalation, so the non-regression
guard should be part of the mask rollout.

Later, if experiments show mask dominates AABB consistently, the AABB branch can be removed
from the final build while keeping it as a fallback.

## 11. Feasibility Contract

The mask solver is allowed to over-reject, never under-reject.

Required invariants:

1. Mask is generated from union footprint buffered by `sqrt(2)/(2R) + eps`.
2. Mask offsets use a global grid frame.
3. Candidate placement shifts masks by exactly `x * R`, `y * R`.
4. If AABB says disjoint, accept disjointness without mask.
5. If AABB overlaps, reject candidate when masks overlap.
6. Final submitted solution is still checked by `utils.check_feasibility`.

Under these invariants:

```text
accepted_by_mask => no true full-footprint polygon overlap
```

The solver remains conservative relative to the current polygon-disjoint footprint rule.

## 12. Validation Plan

### 12.1 Unit tests for geometry safety

For each `R in {4, 8, 16}` and random shape pairs:

1. Generate local masks.
2. Sample random integer placements within plausible bay bounds.
3. If Shapely footprint intersection area `> 1e-9`, assert mask overlap is true.
4. Track false positives where mask overlaps but polygon does not.

Expected:

```text
false_negative_count = 0
false_positive_rate decreases as R increases
```

### 12.2 Slot finder equivalence floor

For every training problem:

- Run AABB-only baseline.
- Run mask with `R=8`.
- Validate both with `check_feasibility`.
- Compare per-bay tardiness and full objective.

Expected:

- invalid count must be `0`.
- mask should improve dense geometry and hard tardiness cases.
- best-of(AABB, mask) should never regress tardiness against AABB for a fixed bay build.

### 12.3 Drift measurement

Measure:

```text
T_aabb
T_mask_R4
T_mask_R8
T_mask_R16
T_polygon
objective
wall_time
candidate_count
mask_overlap_calls
shapely_calls
invalid_count
```

The key metric is not just speed. The real question is whether `T_mask_R8` gets close to
`T_polygon` while staying much cheaper.

### 12.4 Deadline behavior

If precompute time is included inside the contest time limit, it must be budgeted:

```text
if time is tight:
    use R=4 or AABB fallback
else:
    use R=8
```

Precompute should be per problem, before assignment search, and skipped entirely when
`SOLVER_MASK` is off.

## 13. Rollout Steps

1. Implement `MaskProxy` and `_local_mask(bd, orient, R)`.
2. Add a standalone safety test script comparing mask against Shapely.
3. Implement `find_slot_mask`, copied structurally from `find_slot_poly`.
4. Add `solve_bay(..., mask=False, mask_R=8)` path.
5. Add `best-of(AABB, mask)` in final build only.
6. Run training A/B:
   - AABB only
   - polygon escalation
   - mask R=4
   - mask R=8
   - mask R=16
   - best-of(AABB, mask R=8)
   - best-of(AABB, mask R=8, polygon final validation)
7. If invalid count is zero and objective improves, consider using mask in assignment
   scoring as well.

## 14. Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| Rasterization false negative | Infeasible solution | Supercover buffer; random Shapely safety tests; final feasibility checker |
| Too much over-rejection | No quality gain | Tune R; compare R=4/8/16; keep best-of(AABB, mask) |
| Python bit loops too slow | Time regression | AABB y-row prefilter; cache masks; numba or `uint64` packed rows if needed |
| Greedy permissiveness regression | Worse tardiness | Per-bay best-of(AABB, mask), same lesson as polygon escalation |
| Precompute overhead | Less search time | Env gate; deadline-aware R choice; cache once per `(block, orient, R)` |
| Weakening crane invariant | Crane-trap infeasibility | Use union footprint mask, not per-layer masks, in first rollout |

## 15. Initial Recommendation

Start with:

```text
SOLVER_MASK=1
SOLVER_MASK_R=8
mode = best-of(AABB, mask)
scope = final build only
validator = existing polygon feasibility checker
```

Only after the final-build experiment is stable should mask be used in assignment scoring.
That keeps the first rollout low-risk while testing the main hypothesis: a conservative
bitmask can recover most polygon packing gains at a cost much closer to AABB.
