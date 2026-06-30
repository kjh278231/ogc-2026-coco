"""Full-submission-path wall-mode A/B for LAHC -- anchors the comparison to the ACTUAL
shipped search (recombine ON, init_frac=0.6, MIP-repair, idle-ILS), single-process, NOT a
weakened NORECOMB harness. Validates whether LAHC beats the real baseline (memory:
anchor-to-grader-best). Run SEQUENTIALLY -- recombine uses the single-use Gurobi license.

usage: python _lahc_full_ab.py <prob_json> <A|B> <timelimit> [L]
prints: RESULT <name> <cfg> <obj> <feasible> L=<L> <sec>
"""
import json, os, sys, time

prob_path, cfg, tl = sys.argv[1], sys.argv[2], float(sys.argv[3])
L = sys.argv[4] if len(sys.argv) > 4 else "1"

# Mirror myalgorithm.algorithm production gates exactly, but force single-process.
os.environ["SOLVER_MASK_SEARCH"] = "1"
os.environ["SOLVER_MASK"] = "1"
os.environ["SOLVER_ADAPTIVE_RESERVE"] = "1"
os.environ["SOLVER_NUMBA"] = "1"
os.environ["SOLVER_UNIFIED_ILS"] = "1"
os.environ["SOLVER_UNIFIED_INIT_FRAC"] = "0.6"
os.environ["SOLVER_UNIFIED_INIT_CAP"] = "45"
os.environ["SOLVER_MASK_PREPARE"] = "1"
os.environ["SOLVER_IDLE_ILS"] = "1"
os.environ["SOLVER_MIP_REPAIR"] = "1"
os.environ["SOLVER_PORTFOLIO"] = "0"          # force single-process
if cfg == "B":
    os.environ["SOLVER_LAHC"] = "1"
    os.environ["SOLVER_LAHC_L"] = L

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(os.path.dirname(HERE), "bridge")
sys.path.insert(0, BRIDGE)

import myalgorithm, utils

prob = json.load(open(prob_path, encoding="utf-8"))
name = os.path.splitext(os.path.basename(prob_path))[0]
t0 = time.time()
sol = myalgorithm.algorithm(prob, timelimit=tl)
sec = time.time() - t0
chk = utils.check_feasibility(prob, sol)
print("RESULT %s %s %.0f %s L=%s %.1fs" % (
    name, cfg, chk.get("objective", -1), chk.get("feasible"),
    (L if cfg == "B" else "-"), sec), flush=True)
