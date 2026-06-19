# myalgorithm.py -- OGC 2026 ALNS-prototype entry point (separate from submission/).
#
# This copy replaces the production BRIDGE search loop (init + unified ILS + recombine)
# with a kick-centric ALNS engine (see alns.py). It KEEPS the shared infrastructure:
#   - the supercover-mask packing stack (SOLVER_MASK_SEARCH / SOLVER_MASK) so packing
#     feasibility + tightness match the production path,
#   - SOLVER_NUMBA for the jitted scans,
#   - SOLVER_MASK_PREPARE for the fast mask build.
# It deliberately does NOT set the production-only engine gates (UNIFIED_ILS, IDLE_ILS,
# MIP_REPAIR, ADAPTIVE_RESERVE, PORTFOLIO): the ALNS loop owns the search budget so the
# engine A/B is clean. Recombine/MIP can be re-added later as ALNS operators.


def algorithm(prob_info, timelimit=60):
    """Entry point: kick-centric ALNS. Returns {"operations": {...}}."""
    import os
    # Import-time packing gates (read when solver/packing are imported) -- set first.
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    import solver
    try:
        import alns
        return alns.alns_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback: pure AABB build of the
        # most-preferred-bay footprint-disjoint assignment (never crane-trapped).
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH"):
            os.environ.pop(_k, None)
        return solver.build_solution(prob_info, solver.a_pref(prob_info))
