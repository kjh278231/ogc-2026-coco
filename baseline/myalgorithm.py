# myalgorithm.py
# Optimization Grand Challenge 2026 (OGC 2026)
# Developed by Hermes Agent

import time
import random
import math
import contextlib
import os
import sys

from shapely.geometry import Polygon
from shapely.affinity import translate

# Temporarily append the current directory to sys.path to ensure correct imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import baseline_greedy
import utils
from utils import check_feasibility, Bay, Block

# Save original check and utility functions for fallback/final verification
original_check_entry = utils.check_entry
original_check_exit = utils.check_exit
original_check_collisions = utils.check_collisions
original_find_earliest_slot = baseline_greedy._find_earliest_slot

# Global OBB cache: (block_id, orient_idx) -> local OBB (Shapely Polygon)
obb_cache = {}

def precompute_obbs(prob_info):
    """Precompute and cache local OBBs for all block orientations."""
    global obb_cache
    obb_cache = {}
    for bi, blk_data in enumerate(prob_info["blocks"]):
        for oi, orient_data in enumerate(blk_data["shape"]):
            raw_layers = orient_data["layers"]
            layers = utils._resolve_layers(raw_layers)
            if not layers:
                continue
            ref_x, ref_y = layers[0][0] if layers[0] else (0.0, 0.0)
            # Shift all vertices so the reference point is at (0, 0)
            shifted_verts = [[v[0] - ref_x, v[1] - ref_y] for l in layers for v in l]
            poly = Polygon(shifted_verts)
            obb = poly.minimum_rotated_rectangle
            obb_cache[(bi, oi)] = obb

def get_world_obb(block):
    """Retrieve the world-coordinate OBB for a placed block by translating its cached local OBB."""
    local_obb = obb_cache.get((block.block_id, block.orient_idx))
    if local_obb is None:
        return None
    return translate(local_obb, block.x, block.y)

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
        
    new_layers = new_block.layers_at_pos()
    new_bbox   = new_block.bounding_rect()
    n_new      = len(new_layers)
    new_obb    = get_world_obb(new_block)
    
    results = []
    for exist in blocks:
        # Stage 1: AABB Check
        if not utils._bb_overlap(new_bbox, exist.bounding_rect()):
            continue
            
        # Stage 2: OBB Check
        exist_obb = get_world_obb(exist)
        if new_obb is not None and exist_obb is not None:
            if not new_obb.intersects(exist_obb):
                continue
                
        # Stage 3: Full Polygon Check
        exist_layers = exist.layers_at_pos()
        n_exist      = len(exist_layers)
        new_polys = [utils._poly_from_verts(new_layers[k]) for k in range(n_new)]
        for k in range(n_new):
            poly_new = new_polys[k]
            if poly_new is None:
                continue
            for j in range(k, n_exist):
                poly_exist = utils._poly_from_verts(exist_layers[j])
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
    target_layers = target_block.layers_at_pos()
    target_bbox   = target_block.bounding_rect()
    n_target      = len(target_layers)
    target_obb    = get_world_obb(target_block)
    
    results = []
    target_polys = [utils._poly_from_verts(target_layers[k]) for k in range(n_target)]
    
    for exist in blocks:
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
        exist_layers = exist.layers_at_pos()
        n_exist      = len(exist_layers)
        for k in range(n_target):
            poly_target = target_polys[k]
            if poly_target is None:
                continue
            for j in range(k, n_exist):
                poly_exist = utils._poly_from_verts(exist_layers[j])
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
    all_layers = [b.layers_at_pos() for b in blocks]
    obbs = [get_world_obb(b) for b in blocks]
    
    for i in range(n):
        for j in range(i + 1, n):
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
            layers_a = all_layers[i]
            layers_b = all_layers[j]
            
            for k in range(min(len(layers_a), len(layers_b))):
                if layer_indices is not None and k not in layer_indices:
                    continue
                poly_a = utils._poly_from_verts(layers_a[k])
                poly_b = utils._poly_from_verts(layers_b[k])
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
    
    def evaluate_permutation(perm, search_t_limit):
        """Constructs a candidate solution and evaluates its feasibility and objective."""
        bay_placed = [[] for _ in range(n_bays)]
        bay_schedule = [[] for _ in range(n_bays)]
        bay_loads = [0.0] * n_bays
        
        # Build solution using silent greedy placement
        assignments = baseline_greedy._place_blocks(
            perm, blocks_data, bays,
            bay_placed, bay_schedule, bay_loads,
            w1, w2, w3, forced_ids=set(),
            t_start=time.time(), log_interval=0
        )
        
        sol = {"operations": baseline_greedy._build_operations(list(assignments.values()))}
        
        # Repair the solution if there are any violations
        assignments = baseline_greedy._repair(
            prob_info, sol, assignments, bays, blocks_data,
            w1, w2, w3, time.time(), search_t_limit,
            repair_mode="greedy"
        )
        
        final_sol = {"operations": baseline_greedy._build_operations(list(assignments.values()))}
        res = check_feasibility(prob_info, final_sol)
        return res, final_sol

    # 4. Evaluate the entire portfolio and pick the best initial heuristic
    print("[Custom-SA] Evaluating algorithm portfolio of 12 scheduling/packing heuristics...")
    best_obj = float("inf")
    best_sol = None
    best_perm = None
    best_heur_name = "None"
    
    # We run the initial portfolio in silent mode to be as fast as possible
    with silence_stdout():
        for name, perm in heuristics.items():
            res, sol = evaluate_permutation(perm, 5) # brief time for initial check
            if res["feasible"]:
                obj = res["objective"]
                if obj < best_obj:
                    best_obj = obj
                    best_sol = sol
                    best_perm = list(perm)
                    best_heur_name = name

    # Fallback to standard EDD if no heuristic found a feasible solution
    if best_perm is None:
        print("[Custom-SA] Warning: No portfolio heuristic returned a feasible solution. Defaulting to EDD.")
        best_perm = list(heuristics["EDD"])
        baseline_res, baseline_sol = evaluate_permutation(best_perm, timelimit)
        best_obj = baseline_res["objective"] if baseline_res["feasible"] else float("inf")
        best_sol = baseline_sol
    else:
        print(f"[Custom-SA] Chosen Initial Heuristic: {best_heur_name} | Best Objective: {best_obj:.2f}")
    
    # 5. Simulated Annealing local search loop over permutation space
    # Time-based loop ensures we maximize search space while strictly staying within safety bounds
    time_budget = timelimit * 0.90 # 90% of time limit
    
    curr_perm = list(best_perm)
    curr_obj = best_obj
    
    T = 100.0
    cooling_rate = 0.97
    iterations = 0
    improvements = 0
    
    print("[Custom-SA] Starting Simulated Annealing over block permutations...")
    
    # Identify tardy blocks in the current best solution to focus neighborhood search (Limited Local Search)
    tardy_blocks_focused = set()
    if best_sol is not None:
        try:
            with silence_stdout():
                chk_res = check_feasibility(prob_info, best_sol)
                # Find blocks with positive tardiness in the solution
                # (Note: raw tardiness is not directly mapped, but we can infer from exit times)
                # Since we don't have direct maps easily, we can also perturb around any block.
                # To align with the user's Limited Local Search, we focus swaps/shifts on blocks
                # that are closer to violating schedules or are at boundary times.
                pass
        except Exception:
            pass

    while time.time() - start_time < time_budget:
        iterations += 1
        
        # Generate neighbor permutation
        cand_perm = list(curr_perm)
        move_type = random.choice(["swap", "insert", "invert"])
        
        # Limited Local Search Focus: Instead of choosing completely random indices,
        # we give a 50% higher probability to perturbing blocks with tight slack or early due dates
        if random.random() < 0.50 and n_blocks > 3:
            # Pick a block with tight slack
            tight_blocks = sorted(range(n_blocks), key=lambda i: blocks_data[i]["due_date"] - blocks_data[i]["release_time"] - blocks_data[i]["processing_time"])[:max(3, n_blocks // 3)]
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
        remaining_t = time_budget - (time.time() - start_time)
        if remaining_t <= 0:
            break
            
        with silence_stdout():
            res, sol = evaluate_permutation(cand_perm, remaining_t)
            
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
    
    # 6. Restore the original unpatched functions to ensure complete purity of the system
    utils.check_entry = original_check_entry
    utils.check_exit = original_check_exit
    utils.check_collisions = original_check_collisions
    baseline_greedy._find_earliest_slot = original_find_earliest_slot
    
    return best_sol
