# myalgorithm.py
# Optimization Grand Challenge 2026 (OGC 2026)
# Developed by Hermes Agent

import time
import random
import math
import contextlib
import json
import os
import sys

from shapely.geometry import Polygon
from shapely.affinity import translate

# Temporarily append the current directory to sys.path to ensure correct imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import baseline_greedy
import utils
from utils import check_feasibility, Bay, Block

# -----------------------------------------------------------------------------
# Structured JSONL event log (optional). Enabled when the OGC2026_EVENT_LOG
# environment variable is set to a file path; otherwise every emit is a no-op.
# Used by tools/eval_runner.py to capture a parseable trace per (run, instance).
# -----------------------------------------------------------------------------

_event_log_fh = None
_event_log_t0 = None

def _init_event_log(t0: float) -> None:
    global _event_log_fh, _event_log_t0
    _event_log_t0 = t0
    path = os.environ.get("OGC2026_EVENT_LOG")
    if not path:
        _event_log_fh = None
        return
    try:
        _event_log_fh = open(path, "a", buffering=1, encoding="utf-8")
    except Exception:
        _event_log_fh = None

def _close_event_log() -> None:
    global _event_log_fh
    if _event_log_fh is not None:
        try:
            _event_log_fh.close()
        except Exception:
            pass
        _event_log_fh = None

def _emit(event: str, **payload) -> None:
    if _event_log_fh is None:
        return
    try:
        rec = {"t": round(time.time() - _event_log_t0, 4), "event": event}
        rec.update(payload)
        _event_log_fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass

# Save original check and utility functions for fallback/final verification
original_check_entry = utils.check_entry
original_check_exit = utils.check_exit
original_check_collisions = utils.check_collisions
original_find_earliest_slot = baseline_greedy._find_earliest_slot

# Global OBB cache: (block_id, orient_idx) -> local OBB (Shapely Polygon)
obb_cache = {}
# Global per-layer local polygon cache: (block_id, orient_idx) -> list[Polygon|None]
# Each entry is the layer's Shapely Polygon in ref-centered local coordinates,
# so world polys are obtained by a cheap affine translate(local, block.x, block.y)
# instead of reconstructing Shapely geometry per new Block instance.
local_polys_cache = {}

def precompute_obbs(prob_info):
    """Precompute and cache local OBBs *and* per-layer local polygons for all block orientations."""
    global obb_cache, local_polys_cache
    obb_cache = {}
    local_polys_cache = {}
    for bi, blk_data in enumerate(prob_info["blocks"]):
        for oi, orient_data in enumerate(blk_data["shape"]):
            raw_layers = orient_data["layers"]
            layers = utils._resolve_layers(raw_layers)
            if not layers:
                continue
            ref_x, ref_y = layers[0][0] if layers[0] else (0.0, 0.0)
            # Shift every layer so the reference point lands at (0, 0).
            shifted_layers = [[[v[0] - ref_x, v[1] - ref_y] for v in l] for l in layers]
            # Per-layer local polygons — reused by get_world_polys via translate.
            local_polys_cache[(bi, oi)] = [
                utils._poly_from_verts(layer) for layer in shifted_layers
            ]
            # OBB from the union of all layer vertices (same as before).
            shifted_verts = [v for l in shifted_layers for v in l]
            poly = Polygon(shifted_verts)
            obb_cache[(bi, oi)] = poly.minimum_rotated_rectangle

def get_world_obb(block):
    """Retrieve the world-coordinate OBB for a placed block by translating its cached local OBB."""
    if not hasattr(block, "_world_obb"):
        local_obb = obb_cache.get((block.block_id, block.orient_idx))
        if local_obb is None:
            block._world_obb = None
        else:
            block._world_obb = translate(local_obb, block.x, block.y)
    return block._world_obb

def get_world_polys(block):
    """Retrieve the world-coordinate Shapely Polygons for each layer of the block.

    Prefers translating cached local polys (populated by precompute_obbs) — this
    skips Shapely Polygon reconstruction for every fresh Block instance created
    by _place_blocks during SA. Falls back to building from layers_at_pos() if
    the cache is missing (defensive; should not happen in normal flow).
    """
    if not hasattr(block, "_world_polys"):
        local_polys = local_polys_cache.get((block.block_id, block.orient_idx))
        if local_polys is None:
            layers = block.layers_at_pos()
            block._world_polys = [utils._poly_from_verts(layer) for layer in layers]
        else:
            bx, by = block.x, block.y
            block._world_polys = [
                translate(p, bx, by) if p is not None else None
                for p in local_polys
            ]
    return block._world_polys

# -----------------------------------------------------------------------------
# 1. Future EXIT Blocking Prevention Slot Finder (Crane Constraint-Aware)
# -----------------------------------------------------------------------------

def custom_find_earliest_slot(new_blk: Block,
                              bay: Bay,
                              placed_in_bay: list[Block],
                              schedule_in_bay: list[tuple[int, int]],
                              r_time: int,
                              proc: int) -> tuple[int | None, int | None]:
    """
    Enhanced slot finder that adds Future EXIT blocking check.
    Prevents placing new_blk if it blocks the exit of any already-placed block.
    """
    candidate_entries = sorted({r_time} | {e for _, e in schedule_in_bay})

    for entry_candidate in candidate_entries:
        if baseline_greedy._active_deadline_reached():
            return None, None

        entry  = max(r_time, entry_candidate)
        exit_t = entry + proc

        # Stage-2: blocks whose interval [a_k, e_k) contains entry_time
        present_at_entry = [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a <= entry < e
        ]
        if utils.check_entry(bay, present_at_entry, new_blk, fast=True):
            continue  # crane path blocked at entry

        # Stage-3: blocks whose interval [a_k, e_k) strictly contains exit_t
        present_at_exit = [new_blk] + [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a < exit_t < e
        ]
        if utils.check_exit(bay, present_at_exit, new_blk, fast=True):
            continue  # crane path blocked at exit

        # NEW check: Future EXIT blocking prevention! (Crane Constraint-Aware)
        # Check if new_blk blocks the exit of any block exiting between [entry, exit_t)
        future_blocked = False
        for b_other, (a_other, e_other) in zip(placed_in_bay, schedule_in_bay):
            if baseline_greedy._active_deadline_reached():
                return None, None
            if entry < e_other < exit_t:
                # b_other exits while new_blk is already in the bay.
                # Does new_blk block b_other's exit at e_other?
                # We check check_exit on b_other against new_blk
                if utils.check_exit(bay, [new_blk], b_other, fast=True):
                    future_blocked = True
                    break
        if future_blocked:
            continue

        # Stage-4 pre-check: blocks strictly interior to [entry, exit_t)
        s4_blocked = False
        for b_other, (a_other, e_other) in zip(placed_in_bay, schedule_in_bay):
            if baseline_greedy._active_deadline_reached():
                return None, None
            if a_other <= entry or e_other >= exit_t:
                continue  # handled by Stage-2 or Stage-3 above
            if not baseline_greedy._time_overlaps(entry, exit_t, a_other, e_other):
                continue
            if utils.check_collisions(bay, [new_blk, b_other]):
                s4_blocked = True
                break
        if s4_blocked:
            continue

        return entry, exit_t

    return None, None

# -----------------------------------------------------------------------------
# 2. Custom collision and crane check functions with 3-stage hierarchical filtering:
# Stage 1: AABB Pre-filter (extremely fast)
# Stage 2: OBB Pre-filter (tight bounding box for rotated/diagonal shapes)
# Stage 3: Full Polygon intersection (only when bounding boxes overlap)
# -----------------------------------------------------------------------------

def custom_check_entry(bay, blocks, new_block, fast=False):
    if not bay.contains_block(new_block):
        return original_check_entry(bay, blocks, new_block, fast)
        
    new_bbox   = new_block.bounding_rect()
    new_obb    = get_world_obb(new_block)
    new_polys  = get_world_polys(new_block)
    n_new      = len(new_polys)
    
    results = []
    for exist in blocks:
        if baseline_greedy._active_deadline_reached():
            return [None]
        # Stage 1: AABB Check
        if not utils._bb_overlap(new_bbox, exist.bounding_rect()):
            continue
            
        # Stage 2: OBB Check
        exist_obb = get_world_obb(exist)
        if new_obb is not None and exist_obb is not None:
            if not new_obb.intersects(exist_obb):
                continue
                
        # Stage 3: Full Polygon Check
        exist_polys = get_world_polys(exist)
        n_exist     = len(exist_polys)
        for k in range(n_new):
            poly_new = new_polys[k]
            if poly_new is None:
                continue
            for j in range(k, n_exist):
                poly_exist = exist_polys[j]
                if poly_exist is None:
                    continue
                try:
                    inter = poly_new.intersection(poly_exist)
                except Exception:
                    continue
                if not inter.is_empty and inter.area > 0:
                    obs = utils.EntryObstruction(
                        existing_block=exist,
                        new_layer=k,
                        exist_layer=j,
                        intersection=inter,
                    )
                    if fast:
                        return [obs]
                    results.append(obs)
    return results

def custom_check_exit(bay, blocks, target_block, fast=False):
    target_bbox   = target_block.bounding_rect()
    target_obb    = get_world_obb(target_block)
    target_polys  = get_world_polys(target_block)
    n_target      = len(target_polys)
    
    results = []
    for exist in blocks:
        if baseline_greedy._active_deadline_reached():
            return [None]
        if exist.block_id == target_block.block_id:
            continue
            
        # Stage 1: AABB Check
        if not utils._bb_overlap(target_bbox, exist.bounding_rect()):
            continue
            
        # Stage 2: OBB Check
        exist_obb = get_world_obb(exist)
        if target_obb is not None and exist_obb is not None:
            if not target_obb.intersects(exist_obb):
                continue
                
        # Stage 3: Full Polygon Check
        exist_polys = get_world_polys(exist)
        n_exist     = len(exist_polys)
        for k in range(n_target):
            poly_target = target_polys[k]
            if poly_target is None:
                continue
            for j in range(k, n_exist):
                poly_exist = exist_polys[j]
                if poly_exist is None:
                    continue
                try:
                    inter = poly_target.intersection(poly_exist)
                except Exception:
                    continue
                if not inter.is_empty and inter.area > 0:
                    obs = utils.EntryObstruction(
                        existing_block=exist,
                        new_layer=k,
                        exist_layer=j,
                        intersection=inter,
                    )
                    if fast:
                        return [obs]
                    results.append(obs)
    return results

def custom_check_collisions(bay, blocks, layer_indices=None):
    results = []
    n = len(blocks)
    bboxes = [b.bounding_rect() for b in blocks]
    obbs = [get_world_obb(b) for b in blocks]
    
    for i in range(n):
        if baseline_greedy._active_deadline_reached():
            return [None]
        for j in range(i + 1, n):
            if baseline_greedy._active_deadline_reached():
                return [None]
            # Stage 1: AABB Check
            if not utils._bb_overlap(bboxes[i], bboxes[j]):
                continue
                
            # Stage 2: OBB Check
            obb_i = obbs[i]
            obb_j = obbs[j]
            if obb_i is not None and obb_j is not None:
                if not obb_i.intersects(obb_j):
                    continue
                    
            # Stage 3: Full Polygon Check
            ba = blocks[i]
            bb = blocks[j]
            polys_a = get_world_polys(ba)
            polys_b = get_world_polys(bb)
            
            for k in range(min(len(polys_a), len(polys_b))):
                if layer_indices is not None and k not in layer_indices:
                    continue
                poly_a = polys_a[k]
                poly_b = polys_b[k]
                if poly_a is None or poly_b is None:
                    continue
                try:
                    inter = poly_a.intersection(poly_b)
                except Exception:
                    continue
                if not inter.is_empty and inter.area > 0:
                    results.append(utils.CollisionResult(
                        block_a=ba,
                        block_b=bb,
                        layer_index=k,
                        intersection=inter,
                    ))
    return results

# -----------------------------------------------------------------------------
# Utility context manager to silence standard outputs during search
# -----------------------------------------------------------------------------

@contextlib.contextmanager
def silence_stdout():
    with open(os.devnull, 'w') as devnull:
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            yield
        except Exception as e:
            sys.stdout = old_stdout
            raise e
        finally:
            sys.stdout = old_stdout

# -----------------------------------------------------------------------------
# Main Algorithm Entry Point
# -----------------------------------------------------------------------------

def algorithm(prob_info, timelimit=60):
    start_time = time.time()
    _init_event_log(start_time)
    _emit("algo.start", timelimit=float(timelimit))
    hard_deadline = start_time + max(0.0, float(timelimit))
    safety_margin = min(0.5, max(0.05, float(timelimit) * 0.02))

    def remaining_time(deadline=hard_deadline):
        return deadline - time.time()

    def make_timeout_result():
        return {"feasible": False, "stage": "timeout", "objective": float("inf"), "violations": []}
    
    # 1. Precompute OBBs for the problem blocks
    precompute_obbs(prob_info)
    
    # 2. Dynamically monkey-patch utils and baseline functions to boost search and prevent crane blockages
    utils.check_entry = custom_check_entry
    utils.check_exit = custom_check_exit
    utils.check_collisions = custom_check_collisions
    baseline_greedy._find_earliest_slot = custom_find_earliest_slot
    
    bays_data = prob_info["bays"]
    blocks_data = prob_info["blocks"]
    n_bays = len(bays_data)
    n_blocks = len(blocks_data)

    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)
    _emit("algo.context", n_blocks=n_blocks, n_bays=n_bays, w1=w1, w2=w2, w3=w3)
    
    bays = [Bay.from_dict(d, i) for i, d in enumerate(bays_data)]
    
    # 3. Define a portfolio of initial heuristics (including user's Slack-priority combinations)
    heuristics = {}
    
    # Helper to get the area of a block (orientation 0, layer 0 approximation)
    def get_block_area(blk_data):
        layers = blk_data["shape"][0]["layers"]
        if layers and layers[0]:
            return Polygon(layers[0]).area
        return 0.0
        
    # Heuristics 1-5: Standard Priority sorting rules
    heuristics["EDD"] = sorted(
        range(n_blocks),
        key=lambda i: (blocks_data[i]["due_date"], blocks_data[i]["processing_time"])
    )
    heuristics["MST"] = sorted(
        range(n_blocks),
        key=lambda i: (blocks_data[i]["due_date"] - blocks_data[i]["release_time"] - blocks_data[i]["processing_time"], blocks_data[i]["due_date"])
    )
    heuristics["ERD"] = sorted(
        range(n_blocks),
        key=lambda i: (blocks_data[i]["release_time"], blocks_data[i]["due_date"])
    )
    heuristics["LPT"] = sorted(
        range(n_blocks),
        key=lambda i: (-blocks_data[i]["processing_time"], blocks_data[i]["due_date"])
    )
    heuristics["SPT"] = sorted(
        range(n_blocks),
        key=lambda i: (blocks_data[i]["processing_time"], blocks_data[i]["due_date"])
    )
    # Heuristic 6: Largest Footprint Area First
    heuristics["LargestArea"] = sorted(
        range(n_blocks),
        key=lambda i: (-get_block_area(blocks_data[i]), blocks_data[i]["due_date"])
    )
    # Heuristic 7: Midpoint of schedule window
    heuristics["Midpoint"] = sorted(
        range(n_blocks),
        key=lambda i: (blocks_data[i]["release_time"] + blocks_data[i]["due_date"], blocks_data[i]["due_date"])
    )
    # Heuristic 8: Slack-Ratio
    heuristics["SlackRatio"] = sorted(
        range(n_blocks),
        key=lambda i: ((blocks_data[i]["due_date"] - blocks_data[i]["release_time"]) / max(1, blocks_data[i]["processing_time"]), blocks_data[i]["due_date"])
    )
    
    # Heuristics 9-12: Custom parameter combinations of Priority Score (alpha * D_i + beta * slack_i - gamma * P_i)
    # where slack_i = D_i - R_i - P_i
    def eval_priority_score(i, alpha, beta, gamma):
        d = blocks_data[i]["due_date"]
        r = blocks_data[i]["release_time"]
        p = blocks_data[i]["processing_time"]
        slack = d - r - p
        return alpha * d + beta * slack - gamma * p

    heuristics["SlackComb_Balanced"] = sorted(
        range(n_blocks),
        key=lambda i: (eval_priority_score(i, alpha=1.0, beta=1.0, gamma=0.5), blocks_data[i]["due_date"])
    )
    heuristics["SlackComb_SlackHeavy"] = sorted(
        range(n_blocks),
        key=lambda i: (eval_priority_score(i, alpha=0.2, beta=1.0, gamma=0.1), blocks_data[i]["due_date"])
    )
    heuristics["SlackComb_LPT_Heavy"] = sorted(
        range(n_blocks),
        key=lambda i: (eval_priority_score(i, alpha=1.0, beta=0.5, gamma=1.0), blocks_data[i]["due_date"])
    )
    heuristics["SlackComb_HighPriority"] = sorted(
        range(n_blocks),
        key=lambda i: (eval_priority_score(i, alpha=0.5, beta=1.0, gamma=1.0), blocks_data[i]["due_date"])
    )
    
    def evaluate_permutation(perm, search_deadline):
        """Constructs a candidate solution and evaluates its feasibility and objective."""
        if time.time() >= search_deadline:
            return make_timeout_result(), {"operations": {}}

        bay_placed = [[] for _ in range(n_bays)]
        bay_schedule = [[] for _ in range(n_bays)]
        bay_loads = [0.0] * n_bays
        
        # Build solution using silent greedy placement
        assignments = baseline_greedy._place_blocks(
            perm, blocks_data, bays,
            bay_placed, bay_schedule, bay_loads,
            w1, w2, w3, forced_ids=set(),
            t_start=time.time(), log_interval=0,
            deadline=search_deadline
        )
        
        sol = {"operations": baseline_greedy._build_operations(list(assignments.values()))}

        if time.time() >= search_deadline:
            # _place_blocks finished but there's no time for _repair. The greedy
            # placement is often already feasible on easier instances — don't
            # discard it blindly: returning an abstract timeout here causes every
            # init heuristic to look infeasible, which triggers the much worse
            # "best_perm is None" fallback (single EDD retry or forced (0,0)
            # placement). We only run the official scorer when there's enough
            # hard-deadline slack to absorb its cost (Shapely-heavy on big
            # instances). For init-phase calls remaining_time is generous
            # (search_deadline is per-heuristic, far from hard_deadline); for SA
            # iterations remaining_time is ~safety_margin so we still bail.
            if remaining_time() > max(2.0, safety_margin * 4):
                try:
                    res = check_feasibility(prob_info, sol)
                except Exception:
                    res = make_timeout_result()
            else:
                res = make_timeout_result()
            return res, sol

        # Repair the solution if there are any violations
        repair_start = time.time()
        assignments = baseline_greedy._repair(
            prob_info, sol, assignments, bays, blocks_data,
            w1, w2, w3, repair_start, max(0.0, search_deadline - repair_start),
            repair_mode="greedy",
            deadline=search_deadline
        )
        
        final_sol = {"operations": baseline_greedy._build_operations(list(assignments.values()))}
        if time.time() >= search_deadline and remaining_time() <= 0:
            return make_timeout_result(), final_sol

        res = check_feasibility(prob_info, final_sol)
        return res, final_sol

    def evaluate_forced_permutation(perm, search_deadline):
        """Build a quick empty-bay-window fallback solution for all blocks."""
        if time.time() >= search_deadline:
            return make_timeout_result(), {"operations": {}}

        bay_placed = [[] for _ in range(n_bays)]
        bay_schedule = [[] for _ in range(n_bays)]
        bay_loads = [0.0] * n_bays
        assignments = baseline_greedy._place_blocks(
            perm, blocks_data, bays,
            bay_placed, bay_schedule, bay_loads,
            w1, w2, w3, forced_ids=set(perm),
            t_start=time.time(), log_interval=0,
            deadline=search_deadline
        )
        sol = {"operations": baseline_greedy._build_operations(list(assignments.values()))}
        if remaining_time() <= 0:
            return make_timeout_result(), sol
        return check_feasibility(prob_info, sol), sol

    # 4. Evaluate a diversified portfolio and pick the best initial heuristic.
    # EDD / SlackRatio cover due-date and slack-ratio priorities; MST adds an
    # absolute slack ordering; LargestArea brings a geometry-first ordering that
    # tends to win on dense_geometry / crane_trap profiles where the time-only
    # seeds struggle.
    target_heuristics = ["EDD", "SlackRatio", "MST", "LargestArea"]
    print(f"[Custom-SA] Evaluating selected heuristics {target_heuristics}...")
    best_obj = float("inf")
    best_sol = None
    best_perm = None
    best_heur_name = "None"

    # Scale per-heuristic repair budget so the total init phase stays ~30% of
    # timelimit regardless of how many seeds we evaluate (matches the previous
    # 2 * 15% = 30% budget for the 2-heuristic configuration).
    init_check_limit = max(0.5, timelimit * 0.30 / max(1, len(target_heuristics)))
    _emit("init.start", target_heuristics=target_heuristics, init_check_limit=init_check_limit)
    with silence_stdout():
        for name in target_heuristics:
            if remaining_time() <= safety_margin:
                _emit("init.skipped", name=name, reason="no_time")
                break
            if name not in heuristics:
                continue
            perm = heuristics[name]
            search_deadline = min(hard_deadline - safety_margin, time.time() + init_check_limit)
            heur_t0 = time.time()
            res, sol = evaluate_permutation(perm, search_deadline) # brief time for initial check
            _emit("init.heuristic_result",
                  name=name,
                  feasible=bool(res.get("feasible")),
                  stage=res.get("stage"),
                  objective=res.get("objective"),
                  wall_time=round(time.time() - heur_t0, 4))
            if res["feasible"]:
                obj = res["objective"]
                if obj < best_obj:
                    best_obj = obj
                    best_sol = sol
                    best_perm = list(perm)
                    best_heur_name = name

    # Fallback: skip edd_retry (it burned ~20s without progress when all seeds
    # failed at init_check_limit cap) and go straight to forced placement.
    # H-001: forced_direct frees ~18s of SA wall-clock on hard bench_B5 instances.
    if best_perm is None:
        print("[Custom-SA] Warning: No portfolio heuristic returned a feasible solution. Jumping directly to forced placement.")
        best_perm = list(heuristics["EDD"])
        _emit("init.fallback", path="forced_direct", reason="all_seeds_infeasible")
        forced_res, forced_sol = evaluate_forced_permutation(best_perm, hard_deadline)
        best_obj = forced_res["objective"] if forced_res["feasible"] else float("inf")
        best_sol = forced_sol if forced_res["feasible"] else {"operations": {}}
        _emit("init.fallback.outcome", path="forced_direct",
              feasible=bool(forced_res.get("feasible")),
              objective=forced_res.get("objective"))
    else:
        print(f"[Custom-SA] Chosen Initial Heuristic: {best_heur_name} | Best Objective: {best_obj:.2f}")
        _emit("init.chosen", name=best_heur_name, objective=best_obj)
    
    # 5. Simulated Annealing local search loop over permutation space
    # Time-based loop ensures we maximize search space while strictly staying within safety bounds
    search_deadline = min(hard_deadline - safety_margin, start_time + timelimit * 0.90)
    # Per-iteration cap for _repair: prevents one bad neighbor from burning the
    # whole remaining SA budget on a runaway repair pass.
    per_iter_repair_cap = max(2.0, timelimit * 0.05)

    # Precompute focus set for Limited Local Search. blocks_data is immutable
    # during SA, so this was previously redundantly recomputed every iteration.
    tight_blocks = None
    if n_blocks > 3:
        tight_blocks = sorted(
            range(n_blocks),
            key=lambda i: blocks_data[i]["due_date"]
                          - blocks_data[i]["release_time"]
                          - blocks_data[i]["processing_time"]
        )[:max(3, n_blocks // 3)]

    curr_perm = list(best_perm)
    curr_obj = best_obj

    T = 100.0
    cooling_rate = 0.97
    iterations = 0
    improvements = 0

    print("[Custom-SA] Starting Simulated Annealing over block permutations...")

    while time.time() < search_deadline:
        iterations += 1
        
        # Generate neighbor permutation
        cand_perm = list(curr_perm)
        move_type = random.choice(["swap", "insert", "invert"])
        
        # Limited Local Search Focus: Instead of choosing completely random indices,
        # we give a 50% higher probability to perturbing blocks with tight slack or early due dates
        if tight_blocks is not None and random.random() < 0.50:
            # Pick a block with tight slack (precomputed before the loop)
            focus_block = random.choice(tight_blocks)
            idx1 = cand_perm.index(focus_block)
            idx2 = random.randint(0, n_blocks - 1)
        else:
            idx1 = random.randint(0, n_blocks - 1)
            idx2 = random.randint(0, n_blocks - 1)
            
        if move_type == "swap":
            cand_perm[idx1], cand_perm[idx2] = cand_perm[idx2], cand_perm[idx1]
        elif move_type == "insert":
            val = cand_perm.pop(idx1)
            cand_perm.insert(idx2, val)
        elif move_type == "invert":
            if idx1 > idx2:
                idx1, idx2 = idx2, idx1
            cand_perm[idx1:idx2+1] = reversed(cand_perm[idx1:idx2+1])
            
        # Evaluate neighbor candidate in silent mode to avoid console IO overhead
        remaining_t = search_deadline - time.time()
        if remaining_t <= 0:
            break
        # Cap per-iteration repair budget so one expensive neighbor can't
        # consume the whole remaining SA budget.
        iter_budget = min(remaining_t, per_iter_repair_cap)
        if iter_budget <= 0:
            break

        with silence_stdout():
            res, sol = evaluate_permutation(cand_perm, time.time() + iter_budget)
            
        if res["feasible"]:
            obj = res["objective"]
            # SA acceptance condition
            if obj < curr_obj:
                curr_perm = list(cand_perm)
                curr_obj = obj
                if obj < best_obj:
                    best_obj = obj
                    best_sol = sol
                    best_perm = list(cand_perm)
                    improvements += 1
                    print(f"[Custom-SA] Iteration {iterations} ({move_type}): New best objective: {best_obj:.2f}")
                    _emit("sa.improvement",
                          iteration=iterations,
                          move_type=move_type,
                          objective=best_obj)
            else:
                delta = obj - curr_obj
                # Accept worse solution with Boltzmann probability
                prob = math.exp(-delta / max(1.0, T))
                if random.random() < prob:
                    curr_perm = list(cand_perm)
                    curr_obj = obj
                    
        # Cool down temperature
        T *= cooling_rate
        if T < 0.01:
            T = 100.0 # Reheat to escape local minima
            
    print(f"[Custom-SA] Completed search. Total iterations: {iterations} | Improvements: {improvements}")
    print(f"[Custom-SA] Best Objective: {best_obj:.2f}")
    _emit("sa.complete",
          iterations=iterations,
          improvements=improvements,
          best_objective=best_obj)

    # 6. Restore the original unpatched functions to ensure complete purity of the system
    utils.check_entry = original_check_entry
    utils.check_exit = original_check_exit
    utils.check_collisions = original_check_collisions
    baseline_greedy._find_earliest_slot = original_find_earliest_slot

    _emit("algo.end",
          best_objective=best_obj,
          wall_time=round(time.time() - start_time, 4),
          has_solution=best_sol is not None)
    _close_event_log()
    return best_sol
