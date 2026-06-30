# myalgorithm.py -- OGC 2026 submission entry point.
#
# Solver: BRIDGE (bay-decomposed footprint-disjoint admission packer + ILS + guarded
# recombination + escalating packing) with the supercover-bitmask stack:
#   - SOLVER_MASK_SEARCH : the search scores assignments with a conservative supercover
#                          bitmask collision model (near-polygon accuracy at ~AABB cost),
#                          which removes the AABB-proxy drift and explores tight packings.
#   - SOLVER_MASK        : final build escalates AABB -> best-of(AABB, mask).
#   - SOLVER_ADAPTIVE_RESERVE : size the final-build reserve from measured build cost so the
#                          search keeps the otherwise-idle budget.
#   - SOLVER_NUMBA       : numba-jit the AABB candidate scan and the mask overlap test
#                          (behavior-invariant ~1.5x speedup; falls back to pure Python if
#                          numba is unavailable).
#   - SOLVER_MASK_PREPARE: shapely.prepare the buffer before the per-cell point-membership
#                          test in the mask build (~92% of build cost) -> ~4.5x that step,
#                          BIT-IDENTICAL mask (feasibility untouched). At a fixed eval count
#                          the objective is unchanged; the freed wall time becomes more
#                          search at the fixed timelimit.
#   - SOLVER_UNIFIED_ILS : one unified ILS loop (perturb -> best-of(climb, local) -> accept)
#                          replaces the improved->local->ILS pipeline so the perturbation
#                          loop gets the whole post-init budget instead of ~0s (legacy ILS
#                          starved once local consumed the window). INIT_FRAC=0.6 keeps the
#                          short-budget guard floor, while INIT_CAP prevents long timelimits
#                          from spending minutes in the opening convergence phase.
#
# The mask is a SUPERCOVER of the true footprint (mask-disjoint => polygon-disjoint), so
# feasibility is preserved (validated: 0 false negatives). Time-managed; always returns a
# feasible solution. See docs/bitmask_collision_design.md, docs/experiment_board.md.


def algorithm(prob_info, timelimit=60):
    """
    Entry point required by the evaluation server.
    `prob_info`: instance dict (bays, blocks, weights).
    `timelimit`: wall-clock budget in seconds.
    Returns a solution dict {"operations": {...}}.
    """
    import os
    # The mask-search / numba gates are read at solver import time, so set them BEFORE the
    # import. setdefault lets an external environment override any of them for ablation.
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_ADAPTIVE_RESERVE", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_UNIFIED_ILS", "1")
    os.environ.setdefault("SOLVER_UNIFIED_INIT_FRAC", "0.6")
    os.environ.setdefault("SOLVER_UNIFIED_INIT_CAP", "45")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    # Basin-escape via a_pref fresh descent (SOLVER_LAHC). The unified loop's strict-greedy
    # ILS kicks 2-5 blocks from `best` and re-climbs into the SAME basin (small kick + strict
    # descent can't escape), so it stalls in whatever basin improved_search's 2-phase init
    # landed in. SOLVER_LAHC instead re-optimises each kick with _climb_lahc seeded from the
    # RAW a_pref -- a fresh single-phase (Z1+Z2+Z3) descent that reaches far deeper basins.
    # L=1 (neutral-move accept) is the broad winner; the move-history allows the occasional
    # uphill step on the instances that need it. Validated on train T1-T20 (eval-count
    # deterministic) AND the full submission path (recombine ON, wall T=120): beats greedy on
    # every tested instance (-15..-54% full-path; 14W/5T/1L isolated, the lone loss flips to a
    # win once recombine is in play). `best` stays a running min and the final true-objective
    # guard re-scores best-of(LAHC-best, base_incumbent[greedy init]), so the greedy init is
    # kept as the floor. See memory/lahc-basin-escape-win.md, docs/experiment_board.md.
    os.environ.setdefault("SOLVER_LAHC", "1")
    os.environ.setdefault("SOLVER_LAHC_L", "1")
    # Idle reclaim: after recombine, spend leftover wall on more guarded ILS from the
    # current best (init unchanged -> monotonic, can't regress). The reserves are sized for
    # pre-Gurobi/numba costs so ~30% of the budget was idle; this recovers it, targeting the
    # dominant Z3/Z2 assignment cost. Validated T=60: monotonic (incl. prob_20), prob_11
    # -14.8%, prob_6/12 -6%, all feasible, no overrun. Single-process path (workers skip it).
    os.environ.setdefault("SOLVER_IDLE_ILS", "1")
    # MIP-propose + ILS-repair: a Gurobi assignment MIP (min w2*Z2 + w3*Z3) proposes a
    # low-preference-loss basin the single-trajectory ILS can't reach from a_pref, then
    # local_search repairs its tardiness; carried as a guarded best-of candidate. SERIAL by
    # necessity -- the WLS license is single-use (one Gurobi process at a time), so it can't run
    # parallel to the search's recombine. Front-loaded (the search deadlines are t0-based, so it
    # costs a bounded ~8s slice): unlike a post-recombine tail slice it still fires on the
    # biggest winners (which have no idle tail) and never overruns the final build. Validated
    # T=60: prob_11/13/20 -23..-48% (wins dominate), prob_37 +10.9% / prob_26 +4.3% / prob_40
    # +1.5% (search-theft on low-Z3 instances), all feasible, no overrun. Single-process path
    # (workers skip it); mirrors last year's MIP-centric winner.
    os.environ.setdefault("SOLVER_MIP_REPAIR", "1")
    # Multi-order best-of packing (the placement lever). solve_bay's fixed EDD order is not
    # always best; on a tardy bay, best-of {EDD, release, least-slack, area-desc} cuts the
    # contention-driven Z1 (temporal floor is 0, so all Z1 is the packer pushing entries late),
    # which lets the search reach lower-Z3/Z2 assignments. Pareto-safe per bay (EDD always in
    # the set); escalates only on tardy bays. Validated T=180 (true obj): BRIDGE+MO aggregate
    # -34% on the hard family. See bridge/packing.py:solve_bay_best, SOLVER_MO_ORDERS.
    os.environ.setdefault("SOLVER_MULTIORDER", "1")
    # Parallel search portfolio (4 processes -> best-of), gated to T >= MIN_T (default 180).
    # Redesigned for LAHC: an ANCHOR worker runs the exact single-process config (LAHC-L1 +
    # recombine) and the other 3 are NORECOMB diversity (greedy, LAHC-L30, LAHC-L1-more-search);
    # only the anchor recombines during the worker window so the single-use Gurobi license has
    # no contention, the master union-recombines after gather. best-of >= the anchor => the
    # portfolio is Pareto-safe vs single-process and adds the diversity wins (e.g. T20 -27% via
    # the greedy worker) on top. REQUIRES the evaluator to call algorithm() under a __main__
    # guard (spawn re-imports the entry module); any multiprocessing failure is caught and
    # degrades to single-process. See portfolio.py, memory/lahc-basin-escape-win.md.
    os.environ.setdefault("SOLVER_PORTFOLIO", "1")
    _min_t = float(os.environ.get("SOLVER_PORTFOLIO_MIN_T", "180"))
    import solver
    try:
        if os.environ.get("SOLVER_PORTFOLIO") not in (None, "", "0") and timelimit >= _min_t:
            import portfolio
            return portfolio.portfolio_solve(prob_info, timelimit)
        return solver.framework_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback: pure AABB (no shapely/mask) build of the
        # most-preferred-bay, footprint-disjoint assignment (never crane-trapped), exit ASAP.
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_MULTIORDER"):
            os.environ.pop(_k, None)
        return solver.build_solution(prob_info, solver.a_pref(prob_info))
