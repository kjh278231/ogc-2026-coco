"""Athena-local exact geometry helpers.

These mirror the official checker's layer semantics, but reuse Athena's
precomputed local polygons and layer AABBs instead of rebuilding Shapely
polygons from vertices on every candidate check.
"""
from __future__ import annotations

from shapely.affinity import translate

from utils import _bb_overlap

from .features import Features

_WORLD_GEOM_CACHE_CAP = 100_000


def _attrs(obj) -> tuple[int, int, int, int]:
    return (
        int(obj.block_id),
        int(obj.orient_idx),
        int(obj.x),
        int(obj.y),
    )


def _cap_world_cache(F: Features) -> None:
    cache = F.world_geom_cache
    if len(cache) <= _WORLD_GEOM_CACHE_CAP:
        return
    for k in list(cache.keys())[: len(cache) // 5]:
        del cache[k]


def world_geom(F: Features, obj) -> tuple[list[tuple], list] | None:
    """Return world-space (layer_aabbs, layer_polys) for a Block/Assignment."""
    bi, oi, x, y = _attrs(obj)
    key = (bi, oi, x, y)
    cached = F.world_geom_cache.get(key)
    if cached is not None:
        return cached

    local_aabbs = F.layer_aabb.get((bi, oi))
    local_polys = F.local_polys.get((bi, oi))
    if local_aabbs is None or local_polys is None:
        return None

    aabbs = [
        (bb[0] + x, bb[1] + y, bb[2] + x, bb[3] + y)
        for bb in local_aabbs
    ]
    polys = [
        translate(p, xoff=x, yoff=y) if p is not None else None
        for p in local_polys
    ]
    value = (aabbs, polys)
    F.world_geom_cache[key] = value
    _cap_world_cache(F)
    return value


def full_aabb_at(F: Features, bi: int, oi: int, x: int, y: int) -> tuple | None:
    bb = F.aabb.get((int(bi), int(oi)))
    if bb is None:
        return None
    return (bb[0] + x, bb[1] + y, bb[2] + x, bb[3] + y)


def fits_in_bay(F: Features, bay, obj) -> bool:
    bi, oi, x, y = _attrs(obj)
    bb = full_aabb_at(F, bi, oi, x, y)
    if bb is None:
        return False
    return (
        bb[0] >= 0 and bb[1] >= 0
        and bb[2] <= bay.width and bb[3] <= bay.height
    )


def pair_collides_exact(F: Features, a, b) -> bool:
    """Same-height spatial collision, matching utils.check_collisions."""
    ga = world_geom(F, a)
    gb = world_geom(F, b)
    if ga is None or gb is None:
        return True
    a_aabbs, a_polys = ga
    b_aabbs, b_polys = gb
    for k in range(min(len(a_polys), len(b_polys))):
        if not _bb_overlap(a_aabbs[k], b_aabbs[k]):
            continue
        pa = a_polys[k]
        pb = b_polys[k]
        if pa is None or pb is None:
            continue
        try:
            inter = pa.intersection(pb)
        except Exception:
            continue
        if not inter.is_empty and inter.area > 0:
            return True
    return False


def _crane_geoms_obstruct(existing_geom: tuple, target_geom: tuple) -> bool:
    e_aabbs, e_polys = existing_geom
    t_aabbs, t_polys = target_geom
    n_target = len(t_polys)
    n_existing = len(e_polys)
    for k in range(n_target):
        pt = t_polys[k]
        if pt is None:
            continue
        for j in range(k, n_existing):
            if not _bb_overlap(t_aabbs[k], e_aabbs[j]):
                continue
            pe = e_polys[j]
            if pe is None:
                continue
            try:
                inter = pt.intersection(pe)
            except Exception:
                continue
            if not inter.is_empty and inter.area > 0:
                return True
    return False


def crane_obstructs_exact(F: Features, existing, target) -> bool:
    """Return whether `existing` blocks target ENTRY/EXIT under j >= k."""
    ge = world_geom(F, existing)
    gt = world_geom(F, target)
    if ge is None or gt is None:
        return True
    return _crane_geoms_obstruct(ge, gt)


def any_crane_obstructs_exact(F: Features, existing_blocks, target) -> bool:
    """Batch form for one target against many existing blocks."""
    gt = world_geom(F, target)
    if gt is None:
        return True
    for existing in existing_blocks:
        if existing.block_id == target.block_id:
            continue
        ge = world_geom(F, existing)
        if ge is None:
            return True
        if _crane_geoms_obstruct(ge, gt):
            return True
    return False
