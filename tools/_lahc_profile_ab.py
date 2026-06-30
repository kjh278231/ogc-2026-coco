"""Deterministic per-profile eval-count runner for LAHC portfolio-composition study.
One (instance,profile) per FRESH process (memory/perf-ab-one-process-per-instance).

Mirrors the portfolio worker search path (NORECOMB, portfolio off, eval-count fixed) so
the per-profile objectives are directly comparable and best-of subsets approximate what
the master's best-of would pick. Profiles match portfolio.PROFILES + diverse candidates.

usage: python _lahc_profile_ab.py <prob_json> <profile> <max_evals>
  profile in: anc grd l30 i01 div div01
prints: RESULT <name> <profile> <obj> <feasible> evals=<N> <sec>
"""
import json, os, sys, time

prob_path, profile, max_evals = sys.argv[1], sys.argv[2], sys.argv[3]

# Import-time + search-path gates (mirror myalgorithm.algorithm), minus wall-only levers.
os.environ["SOLVER_MASK_SEARCH"] = "1"
os.environ["SOLVER_MASK"] = "1"
os.environ["SOLVER_NUMBA"] = "1"
os.environ["SOLVER_UNIFIED_ILS"] = "1"
os.environ["SOLVER_MASK_PREPARE"] = "1"
os.environ["SOLVER_MAX_EVALS"] = max_evals
os.environ["SOLVER_NORECOMB"] = "1"
os.environ["SOLVER_PORTFOLIO"] = "0"

PROFILES = {
    "anc":   {"SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1"},                       # anchor = single-process (kick re-seed)
    "grd":   {"SOLVER_LAHC": "0"},                                            # greedy
    "l30":   {"SOLVER_LAHC": "1", "SOLVER_LAHC_L": "30"},                      # LAHC L30 (uphill)
    "i01":   {"SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1", "SOLVER_LAHC_INIT_FRAC": "0.1"},
    "div":   {"SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1", "SOLVER_LAHC_DIVERSE": "1"},
    "div01": {"SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1", "SOLVER_LAHC_DIVERSE": "1",
              "SOLVER_LAHC_INIT_FRAC": "0.1"},
}
for k, v in PROFILES[profile].items():
    os.environ[k] = v

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(os.path.dirname(HERE), "bridge")
sys.path.insert(0, BRIDGE)

import solver, utils

prob = json.load(open(prob_path, encoding="utf-8"))
name = os.path.splitext(os.path.basename(prob_path))[0]
t0 = time.time()
sol = solver.framework_solve(prob, 60)
sec = time.time() - t0
chk = utils.check_feasibility(prob, sol)
print("RESULT %s %s %.0f %s evals=%s %.1fs" % (
    name, profile, chk.get("objective", -1), chk.get("feasible"), max_evals, sec), flush=True)
