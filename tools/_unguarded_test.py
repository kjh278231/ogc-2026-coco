"""UNGUARDED entry (algorithm called at module level, NO __main__ guard) -- simulates an
evaluator that does not guard. With the spawn-probe in portfolio_solve, this MUST degrade
cleanly to single-process (good obj), NOT the chaotic recursive-spawn fallback (~1.4M).
usage: python _unguarded_test.py <prob_json> <timelimit>"""
import json, os, sys, time
prob_path, tl = sys.argv[1], float(sys.argv[2])
os.environ["SOLVER_PORTFOLIO"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge"))
import myalgorithm, utils
prob = json.load(open(prob_path, encoding="utf-8"))
t0 = time.time()
sol = myalgorithm.algorithm(prob, timelimit=tl)
sec = time.time() - t0
chk = utils.check_feasibility(prob, sol)
print("UNGUARDED obj=%.0f feasible=%s %.1fs" % (chk.get("objective", -1), chk.get("feasible"), sec), flush=True)
