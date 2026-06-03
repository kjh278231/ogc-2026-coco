"""Feature precomputation and global time-window smoothing for Athena."""
from __future__ import annotations

import math

from shapely.geometry import Polygon

from utils import Bay, _bounding_box, _poly_from_verts, _resolve_layers

# -----------------------------------------------------------------------------
# Phase 1 -- feature precomputation
# -----------------------------------------------------------------------------

class Features:
    __slots__ = (
        "aabb", "obb_local", "local_polys",
        "layer_aabb", "world_geom_cache",
        "n_layers", "area_top", "area_sum", "crane_risk",
        "dims", "bay_fit", "anchor_bounds", "safe_anchor",
    )

    def __init__(self) -> None:
        self.aabb: dict = {}          # (bi, oi) -> (minx, miny, maxx, maxy)
        self.obb_local: dict = {}     # (bi, oi) -> Shapely Polygon (local)
        self.local_polys: dict = {}   # (bi, oi) -> list[Polygon|None]
        self.layer_aabb: dict = {}    # (bi, oi) -> list[(minx, miny, maxx, maxy)]
        self.world_geom_cache: dict = {}  # (bi, oi, x, y) -> (layer_aabb, layer_polys)
        self.n_layers: dict = {}      # (bi, oi) -> int
        self.area_top: dict = {}      # (bi, oi) -> float (layer 0 area)
        self.area_sum: dict = {}      # (bi, oi) -> float (sum of per-layer areas)
        self.crane_risk: dict = {}    # (bi, oi) -> float
        self.dims: dict = {}          # (bi, oi) -> (width, height)
        self.bay_fit: dict = {}       # (bi, oi) -> list[bay_id]
        self.anchor_bounds: dict = {} # (bi, oi, bay_id) -> (x_lo, x_hi, y_lo, y_hi)
        self.safe_anchor: dict = {}   # (bi, oi, bay_id) -> (x, y)


def _anchor_bounds_from_aabb(bay: Bay, bb: tuple) -> tuple[int, int, int, int] | None:
    lx0, ly0, lx1, ly1 = bb
    x_lo = int(math.ceil(-lx0 - 1e-9))
    y_lo = int(math.ceil(-ly0 - 1e-9))
    x_hi = int(math.floor(bay.width - lx1 + 1e-9))
    y_hi = int(math.floor(bay.height - ly1 + 1e-9))
    if x_lo > x_hi or y_lo > y_hi:
        return None
    return x_lo, x_hi, y_lo, y_hi


def precompute_features(prob_info: dict, bays: list[Bay]) -> Features:
    F = Features()
    for bi, blk in enumerate(prob_info["blocks"]):
        for oi, orient in enumerate(blk["shape"]):
            raw_layers = orient.get("layers", [])
            layers = _resolve_layers(raw_layers)
            if not layers:
                continue
            ref_x, ref_y = layers[0][0]
            shifted = [
                [[v[0] - ref_x, v[1] - ref_y] for v in l]
                for l in layers
            ]
            all_v = [v for l in shifted for v in l]
            bb = _bounding_box(all_v)
            F.aabb[(bi, oi)] = bb
            F.dims[(bi, oi)] = (bb[2] - bb[0], bb[3] - bb[1])

            polys = [_poly_from_verts(layer) for layer in shifted]
            F.local_polys[(bi, oi)] = polys
            F.layer_aabb[(bi, oi)] = [_bounding_box(layer) for layer in shifted]

            areas = [(p.area if p is not None else 0.0) for p in polys]
            F.area_top[(bi, oi)] = areas[0] if areas else 0.0
            F.area_sum[(bi, oi)] = sum(areas)
            F.n_layers[(bi, oi)] = len(layers)
            F.crane_risk[(bi, oi)] = len(layers) * (areas[0] if areas else 1.0)

            try:
                F.obb_local[(bi, oi)] = Polygon(all_v).minimum_rotated_rectangle
            except Exception:
                F.obb_local[(bi, oi)] = None

            fit = []
            w, h = F.dims[(bi, oi)]
            for bid, bay in enumerate(bays):
                if w <= bay.width + 1e-6 and h <= bay.height + 1e-6:
                    fit.append(bid)
                bounds = _anchor_bounds_from_aabb(bay, bb)
                if bounds is None:
                    continue
                F.anchor_bounds[(bi, oi, bid)] = bounds
                x_lo, _x_hi, y_lo, _y_hi = bounds
                F.safe_anchor[(bi, oi, bid)] = (x_lo, y_lo)
            F.bay_fit[(bi, oi)] = fit
    return F


# -----------------------------------------------------------------------------
# Phase 2 -- global time-window smoothing
# -----------------------------------------------------------------------------

def smooth_time_windows(prob_info: dict, F: Features,
                        max_cands_per_block: int = 60,
                        alpha_peak: float = 0.4,
                        beta_var: float = 1e-3,
                        gamma_tard: float = 4.0) -> tuple[list[int], list[int]]:
    """Return (target_entry, target_orient) lists indexed by block_id.

    Block processing order is least-slack-first. For each block, candidate
    entry times in [release, due - proc] are sampled (capped at
    max_cands_per_block) plus a few tardy options. Cost combines added peak
    load, variance contribution, and objective-aware tardiness/delay risk.
    """
    blocks = prob_info["blocks"]
    n = len(blocks)
    if n == 0:
        return [], []

    weights = prob_info.get("weights", {})
    w1 = float(weights.get("w1", 1.0))
    # Tardiness dominates the official objective on the training set. Keep the
    # smoothing scale bounded so congestion still matters inside the on-time
    # window, but make explicitly tardy candidates expensive enough that they
    # are not chosen just to shave a small peak-load bump.
    tard_weight = max(gamma_tard, min(2000.0, 0.05 * w1))
    base_delay_weight = min(0.10 * tard_weight, 0.001 * w1)

    horizon = max(b["due_date"] + b["processing_time"] for b in blocks) + 8
    load = [0.0] * (horizon + 1)

    target_entry = [0] * n
    target_orient = [0] * n

    order = sorted(
        range(n),
        key=lambda i: (
            blocks[i]["due_date"] - blocks[i]["release_time"] - blocks[i]["processing_time"],
            blocks[i]["due_date"],
        ),
    )

    for bi in order:
        b = blocks[bi]
        r = int(b["release_time"])
        d = int(b["due_date"])
        p = int(b["processing_time"])
        w = float(b["workload"])
        slack = d - r - p
        slack_pressure = 2.0 / max(2.0, float(max(0, slack) + 2))
        delay_weight = base_delay_weight * slack_pressure

        lo = r
        hi = max(r, d - p)
        n_cands = hi - lo + 1
        if n_cands > max_cands_per_block:
            step = max(1, n_cands // max_cands_per_block)
            cands = list(range(lo, hi + 1, step))
            if cands[-1] != hi:
                cands.append(hi)
        else:
            cands = list(range(lo, hi + 1))
        # also add a few "tardy-but-cheap" candidates in case feasible window is tight
        for delta in (1, 3, 7):
            cands.append(hi + delta)

        best_cost = float("inf")
        best_e = lo
        for e in cands:
            if e < r:
                continue
            tard = max(0, e + p - d)
            peak = 0.0
            var_inc = 0.0
            t1 = e + p
            for t in range(e, t1):
                if t >= len(load):
                    continue
                old = load[t]
                new = old + w
                if new > peak:
                    peak = new
                var_inc += new * new - old * old
            delay = max(0, e - r)
            cost = (alpha_peak * peak
                    + beta_var * var_inc
                    + tard_weight * tard
                    + delay_weight * delay)
            if cost < best_cost:
                best_cost = cost
                best_e = e

        target_entry[bi] = best_e
        # pick the most square-ish orientation as the default initial guess
        best_oi = 0
        best_ratio = float("inf")
        for oi in range(len(b["shape"])):
            dims = F.dims.get((bi, oi))
            if dims is None:
                continue
            w_d, h_d = dims
            ratio = max(w_d, h_d) / max(1.0, min(w_d, h_d))
            if ratio < best_ratio:
                best_ratio = ratio
                best_oi = oi
        target_orient[bi] = best_oi

        for t in range(best_e, best_e + p):
            if 0 <= t < len(load):
                load[t] += w

    return target_entry, target_orient
