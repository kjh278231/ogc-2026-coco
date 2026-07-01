# myalgorithm.py -- OGC 2026 entry point for the PRISM solver (third algorithm).
#
# PRISM: a spectrum of preference-ideal MIP anchors -> memetic LAHC refinement of each
# -> best-of + guarded recombination. Distinct from BRIDGE (single a_pref-anchored ILS/
# LAHC), ALNS (LNS), and the covering set-partition prototype. See prism/solver.py and
# docs/experiment_board.md for the diagnosis that motivated it.
def algorithm(prob_info, timelimit=60):
    import os
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    # Multi-order best-of packing (the placement lever -- see docs/placement, stow log). The
    # EDD pack order is not always best; on a tardy bay, best-of {EDD, release, least-slack,
    # area-desc} cuts the contention-driven Z1, which lets the MIP-anchor+LAHC search settle on
    # lower-Z3/Z2 assignments. Set on the master AND inherited by the spawned portfolio workers
    # (os.environ propagates to spawn children; no worker unsets it). DECISIVE on the hard family
    # at T=180 (true obj, vs PRISM without it): T1 32188->6533, T11 38262->32039, T13 168579->
    # 111752, T18 72839->65949, T20 223903->149050 -- 6/6 wins, aggregate -31%. Pareto-safe per
    # bay (EDD always in the set). See bridge/packing.py:solve_bay_best, SOLVER_MO_ORDERS.
    os.environ.setdefault("SOLVER_MULTIORDER", "1")
    # Z1=0 phase-transition Z3 refinement via guided SWAP moves (see prism_engine._refine ->
    # K._z3_refine). Once a descent reaches Z1=0, the near-lexicographic objective means the game
    # is minimising Z3 while keeping Z1=0; the relocation-only search + recombine miss mutual-
    # preference exchanges that swaps reach. Validated: swaps improve the full recombined
    # multi-order solution -6..-54% (tools/_swap_probe2.py), and PRISM+MO eval-count T13
    # 404,238 -> 117,545 (-71%) with SWAP on. Pareto-safe; env-rollbackable.
    os.environ.setdefault("SOLVER_SWAP", "1")
    os.environ.setdefault("PRISM_PORTFOLIO", "1")
    # Gate lowered 180 -> 45: the anchor-portfolio gives each of 4 workers the FULL budget on
    # its own core, so it BEATS the single-process even-split at every budget tested (T=60:
    # T20 -59%, T17 -22%, T13 -15%; T=120: T20 -44%; T=180: full-20 -6.9%). The old "T=60
    # portfolio regressed" verdict was the budget-SPLIT V1, which does not apply here. Floor=45
    # (proven win at T>=60; T=40 stays feasible/no-overrun but heavy-packing quality dips, so
    # avoid going lower) keeps spawn+warm-build (~5s) a small fraction; the spawn-probe still
    # falls back to serial on spawn-unsafe graders. See docs/prism_experiment_log.md (P*b).
    _min_t = float(os.environ.get("PRISM_PORTFOLIO_MIN_T", "45"))
    import prism_engine as prism
    try:
        if os.environ.get("PRISM_PORTFOLIO") not in (None, "", "0") and timelimit >= _min_t:
            import portfolio
            return portfolio.portfolio_solve(prob_info, timelimit)
        return prism.prism_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback (pure AABB, most-preferred bay).
        import solver as K
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_MULTIORDER", "SOLVER_SWAP"):
            os.environ.pop(_k, None)
        return K.build_solution(prob_info, K.a_pref(prob_info))
