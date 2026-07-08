# myalgorithm.py -- OGC 2026 entry point for the HELM solver (sixth algorithm).
#
# HELM: instance-adaptive anchor-spectrum ROUTING. Every prior mechanism (PRISM anchors,
# FLUX congestion anchors, WEAVE population, ejection, guided destroy, MIP seed) proved
# instance-split -- each wins one regime and loses another, and 4 cores force a trade-off.
# HELM measures the regime from cheap signals (peak demand-window footprint utilisation,
# energetic-reasoning floor rho) and steers the shared portfolio to the spectrum that WON
# that regime head-to-head. See helm/helm_engine.py + docs/newsolver_helm_design.md.
def algorithm(prob_info, timelimit=60):
    import os
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    # Multi-order best-of packing: on a tardy bay, best-of {EDD, release, least-slack,
    # area-desc} cuts contention-driven Z1 (the biggest grader lever to date). Pareto-safe
    # (EDD always in the set). Inherited by spawned portfolio workers via os.environ.
    os.environ.setdefault("SOLVER_MULTIORDER", "1")
    # Z1=0 phase-transition Z3 refinement via guided SWAP: once a descent reaches Z1=0 the
    # near-lexicographic objective makes it a Z3-minimisation game; swaps reach mutual-
    # preference exchanges relocation+recombine miss. Pareto-safe; env-rollbackable.
    os.environ.setdefault("SOLVER_SWAP", "1")
    os.environ.setdefault("HELM_PORTFOLIO", "1")
    # Portfolio gate: one routed anchor per worker, each with the full budget on its own
    # core (beats a serial even-split at every budget tested). Floor 45s as PRISM/FLUX.
    _min_t = float(os.environ.get("HELM_PORTFOLIO_MIN_T", "45"))
    import helm_engine as helm
    try:
        if os.environ.get("HELM_PORTFOLIO") not in (None, "", "0") and timelimit >= _min_t:
            import portfolio
            return portfolio.portfolio_solve(prob_info, timelimit)
        return helm.helm_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback (pure AABB, most-preferred bay).
        import solver as K
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_MULTIORDER", "SOLVER_SWAP"):
            os.environ.pop(_k, None)
        return K.build_solution(prob_info, K.a_pref(prob_info))
