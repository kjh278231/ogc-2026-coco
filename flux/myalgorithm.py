# myalgorithm.py -- OGC 2026 entry point for the FLUX solver (fifth algorithm).
#
# FLUX: the first solver to model PACKING CONGESTION at the ASSIGNMENT stage. Its anchors
# minimise w3*Z3 + w2*Z2 subject to a spatial-temporal footprint-capacity signal (peak demand-
# window footprint-area utilisation), so they are near-packable (low Z1) by construction. This
# targets the Z1-DOMINATED heavy >=300s instances (P4/P5/P6 zone), where every existing solver's
# anchor is packing-blind. See flux/flux_engine.py + docs/newsolver_flux_design.md.
def algorithm(prob_info, timelimit=60):
    import os
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    # Multi-order best-of packing: on a tardy bay, best-of {EDD, release, least-slack, area-desc}
    # cuts contention-driven Z1 (the biggest grader lever to date). Pareto-safe (EDD always in the
    # set). Inherited by spawned portfolio workers via os.environ.
    os.environ.setdefault("SOLVER_MULTIORDER", "1")
    # Z1=0 phase-transition Z3 refinement via guided SWAP: once a descent reaches Z1=0 the near-
    # lexicographic objective makes it a Z3-minimisation game; swaps reach mutual-preference
    # exchanges relocation+recombine miss. Pareto-safe; env-rollbackable.
    os.environ.setdefault("SOLVER_SWAP", "1")
    os.environ.setdefault("FLUX_PORTFOLIO", "1")
    # Portfolio gate: like PRISM, each worker gets the full budget on its own core (one anchor per
    # worker), which beats a serial even-split at every budget. The master computes all anchors once
    # (the single mipcong anchor uses the single-use WLS license serially; the greedy anchors are
    # Gurobi-free) and workers run pure NORECOMB LAHC. Floor 45s (spawn+warm-build ~5s stays small).
    _min_t = float(os.environ.get("FLUX_PORTFOLIO_MIN_T", "45"))
    import flux_engine as flux
    try:
        if os.environ.get("FLUX_PORTFOLIO") not in (None, "", "0") and timelimit >= _min_t:
            import portfolio
            return portfolio.portfolio_solve(prob_info, timelimit)
        return flux.flux_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback (pure AABB, most-preferred bay).
        import solver as K
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_MULTIORDER", "SOLVER_SWAP"):
            os.environ.pop(_k, None)
        return K.build_solution(prob_info, K.a_pref(prob_info))
