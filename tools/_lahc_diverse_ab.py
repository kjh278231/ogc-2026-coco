"""Deterministic A/B for LAHC restart-diversity (SOLVER_LAHC_DIVERSE), one
(instance,config) per FRESH process (avoids id()-keyed cache contamination --
see memory/perf-ab-one-process-per-instance).

Eval-count mode (SOLVER_MAX_EVALS) -> deterministic objective. Recombine OFF and
portfolio OFF so the comparison isolates the LAHC trajectory:
  A = current LAHC (single deterministic a_pref walk, then kick-of-best re-seeds)
  B = diverse LAHC (every walk re-seeds from a_pref with a per-walk shuffled mover order)
SOLVER_LAHC_DEBUG=1 prints walk count to stderr so we can see if diversity engaged.

usage: python _lahc_diverse_ab.py <prob_json> <A|B> <max_evals> [L]
prints: RESULT <name> <cfg> <obj> <feasible> evals=<N> L=<L> <sec>
"""
import json, os, sys, time

prob_path, cfg, max_evals = sys.argv[1], sys.argv[2], sys.argv[3]
L = sys.argv[4] if len(sys.argv) > 4 else "1"

# Submission search-path gates (mirror myalgorithm.algorithm), minus the wall-only levers.
os.environ["SOLVER_MASK_SEARCH"] = "1"
os.environ["SOLVER_MASK"] = "1"
os.environ["SOLVER_NUMBA"] = "1"
os.environ["SOLVER_UNIFIED_ILS"] = "1"
os.environ["SOLVER_MASK_PREPARE"] = "1"
# Isolation: deterministic eval budget, no recombine, no portfolio.
os.environ["SOLVER_MAX_EVALS"] = max_evals
os.environ["SOLVER_NORECOMB"] = "1"
os.environ["SOLVER_PORTFOLIO"] = "0"
# Both configs run LAHC (the proven lever); B adds restart diversity.
os.environ["SOLVER_LAHC"] = "1"
os.environ["SOLVER_LAHC_L"] = L
os.environ["SOLVER_LAHC_DEBUG"] = "1"
if cfg == "B":
    os.environ["SOLVER_LAHC_DIVERSE"] = "1"

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
print("RESULT %s %s %.0f %s evals=%s L=%s %.1fs" % (
    name, cfg, chk.get("objective", -1), chk.get("feasible"),
    max_evals, L, sec), flush=True)
