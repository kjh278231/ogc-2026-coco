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
    """Entry point: kick-centric ALNS. Returns {"operations": {...}}.

    BEST-OF BUILD (timelimit-gated portfolio):
      * timelimit >= _PORTFOLIO_MIN_T (180s) -> best-of(config A, lever) portfolio:
        run the config-A trajectory (regression floor) AND the MIP-seed+recombine
        lever trajectory in parallel, true-score both, emit the better. Provably
        >= both -> P3/P5-type keep lever gains, P4/P6/T1-type protected from the
        lever regression (see memory/alns-seed-recombine-instance-split).
      * timelimit  < _PORTFOLIO_MIN_T -> single-process config A (levers off): on
        short budgets the levers don't pay and the portfolio spawn/warm overhead
        hurts, so just run the floor.
    Any multiprocessing failure degrades to a single-process config-A solve.
    """
    import os
    # Import-time packing gates (read when solver/packing are imported) -- set first.
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    import solver
    _PORTFOLIO_MIN_T = float(os.environ.get("ALNS_PORTFOLIO_MIN_T", "180"))
    try:
        import alns
        if timelimit >= _PORTFOLIO_MIN_T:
            import alns_portfolio
            return alns_portfolio.portfolio_solve(prob_info, timelimit)
        # short budget: config-A floor, single process (levers explicitly off).
        os.environ["ALNS_MIP_SEED"] = "0"
        os.environ["ALNS_RECOMB"] = "off"
        # tight reserves so wall stays within ~5s of the budget (no overrun): small
        # end-buffer + a final-build margin just above the measured build cost.
        os.environ.setdefault("ALNS_SAFETY_FRAC", "0.02")
        os.environ.setdefault("ALNS_BUILD_MARGIN_FACTOR", "1.5")
        return alns.alns_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback: pure AABB build of the
        # most-preferred-bay footprint-disjoint assignment (never crane-trapped).
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH"):
            os.environ.pop(_k, None)
        return solver.build_solution(prob_info, solver.a_pref(prob_info))
