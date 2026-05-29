import argparse
import json
import os
import sys
import time
import pathlib
from collections import defaultdict

# Add baseline folder to path so we can import myalgorithm and baseline_greedy
sys.path.insert(0, str(pathlib.Path(__file__).parent / "baseline"))

import baseline_greedy
import myalgorithm
import utils

def main():
    parser = argparse.ArgumentParser(description="Evaluate OGC 2026 algorithms on benchmark suite")
    parser.add_argument("--timelimit", type=float, default=60.0,
                        help="time limit per instance for myalgorithm (default: 60.0)")
    parser.add_argument("--greedy-timelimit", type=float, default=10.0,
                        help="time limit for baseline greedy (default: 10.0)")
    parser.add_argument("--pattern", type=str, default="*.json",
                        help="glob pattern for benchmark files (default: *.json)")
    args = parser.parse_args()

    benchmark_dir = pathlib.Path(__file__).parent / "alg_tester" / "example" / "benchmark"
    json_files = sorted(benchmark_dir.glob(args.pattern))
    
    if not json_files:
        print("No benchmark JSON files found in", benchmark_dir)
        return

    print("=" * 110)
    print(f"Evaluation started with myalgorithm limit = {args.timelimit}s, greedy limit = {args.greedy_timelimit}s")
    print(f"Found {len(json_files)} benchmark files.")
    print("=" * 110)
    print(f"{'Instance':<35} | {'Algorithm':<15} | {'Feasible':<8} | {'Stage':<6} | {'Objective':<12} | {'Time (s)':<8}")
    print("=" * 110)

    for json_file in json_files:
        # Load the problem instance
        with open(json_file) as f:
            prob_info = json.load(f)
        
        name = json_file.stem
        
        # 1. Test Baseline Greedy
        # Re-set original functions just in case they were monkey-patched
        utils.check_entry = myalgorithm.original_check_entry
        utils.check_exit = myalgorithm.original_check_exit
        utils.check_collisions = myalgorithm.original_check_collisions
        baseline_greedy._find_earliest_slot = myalgorithm.original_find_earliest_slot
        
        t0 = time.time()
        try:
            sol_greedy = baseline_greedy.greedyalgorithm(prob_info, timelimit=args.greedy_timelimit)
            elapsed_greedy = time.time() - t0
            res_greedy = utils.check_feasibility(prob_info, sol_greedy)
            feasible_g = res_greedy["feasible"]
            stage_g = res_greedy["stage"]
            obj_g = f"{res_greedy['objective']:.2f}" if feasible_g else "N/A"
        except Exception as e:
            elapsed_greedy = time.time() - t0
            feasible_g = "ERROR"
            stage_g = "N/A"
            obj_g = str(e)[:15]
        
        # 2. Test MyAlgorithm (Hermes)
        t0 = time.time()
        try:
            sol_my = myalgorithm.algorithm(prob_info, timelimit=args.timelimit)
            elapsed_my = time.time() - t0
            res_my = utils.check_feasibility(prob_info, sol_my)
            feasible_m = res_my["feasible"]
            stage_m = res_my["stage"]
            obj_m = f"{res_my['objective']:.2f}" if feasible_m else "N/A"
        except Exception as e:
            elapsed_my = time.time() - t0
            feasible_m = "ERROR"
            stage_m = "N/A"
            obj_m = str(e)[:15]

        # Print comparison
        print(f"{name:<35} | {'Baseline (G)':<15} | {str(feasible_g):<8} | {str(stage_g):<6} | {obj_g:<12} | {elapsed_greedy:.3f}")
        print(f"{'':<35} | {'MyAlgorithm (H)':<15} | {str(feasible_m):<8} | {str(stage_m):<6} | {obj_m:<12} | {elapsed_my:.3f}")
        print("-" * 110)

if __name__ == "__main__":
    main()
