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
#   - SOLVER_UNIFIED_ILS : one unified ILS loop (perturb -> best-of(climb, local) -> accept)
#                          replaces the improved->local->ILS pipeline so the perturbation
#                          loop gets the whole post-init budget instead of ~0s (legacy ILS
#                          starved once local consumed the window). INIT_FRAC=0.6 reserves
#                          the opening 60% to fully converge the trusted incumbent (guard
#                          floor) before perturbing. full-20 wall A/B: -9% net, all feasible,
#                          no dangerous regression (worst +4.9%).
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
    import solver
    try:
        return solver.framework_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback: pure AABB (no shapely/mask) build of the
        # most-preferred-bay, footprint-disjoint assignment (never crane-trapped), exit ASAP.
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH"):
            os.environ.pop(_k, None)
        return solver.build_solution(prob_info, solver.a_pref(prob_info))
