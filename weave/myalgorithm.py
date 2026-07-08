# myalgorithm.py -- OGC 2026 entry point for WEAVE (fourth solver).
#
# WEAVE: a co-evolving POPULATION on assignment vectors {block_id: bay_id}. Diverse Z1=0
# elites (heuristic + MIP preference-ideal anchors) exchange assignment STRUCTURE during
# search via crossover (best x max-Hamming partner) + optional path-relinking, refined by
# LAHC + guided swap + an ejection-chain local search (k-opt generalisation of swap), then a
# guarded column-recombination and a Pareto-safe final best-of. Distinct from BRIDGE (single
# ILS/LAHC), PRISM (independent anchor refinement + best-of), STOW (packing portfolio).
# See weave/weave_engine.py and docs/newsolver_experiment_design.md.
def algorithm(prob_info, timelimit=60):
    import os
    # packing / scoring stack (same validated defaults as PRISM+MO)
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    os.environ.setdefault("SOLVER_MULTIORDER", "1")   # multi-order best-of packing (Pareto-safe)
    os.environ.setdefault("SOLVER_SWAP", "1")         # Z1=0 phase-transition Z3 swap refinement
    # WEAVE knobs (rollback-able): ejection-chain local search + population size.
    os.environ.setdefault("WEAVE_EJECTION", "1")
    os.environ.setdefault("WEAVE_PORTFOLIO", "1")   # island model on >= MIN_T (4 cores, full budget/island)
    _min_t = float(os.environ.get("WEAVE_PORTFOLIO_MIN_T", "45"))
    try:
        if os.environ.get("WEAVE_PORTFOLIO") not in (None, "", "0") and timelimit >= _min_t:
            import portfolio
            return portfolio.portfolio_solve(prob_info, timelimit)
        import weave_engine as WE
        return WE.weave_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback (pure AABB, most-preferred bay)
        import sys, os as _os
        _b = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "bridge")
        if _b not in sys.path:
            sys.path.append(_b)
        import solver as K
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_MULTIORDER", "SOLVER_SWAP"):
            os.environ.pop(_k, None)
        return K.build_solution(prob_info, K.a_pref(prob_info))
