# myalgorithm.py -- OGC 2026 submission entry point (ACCORD).
#
# Solver: ACCORD -- per-block congestion-price consensus (ADMM / surrogate-Lagrangian /
# tatonnement family). Iterates {price-augmented assignment MIP -> true per-bay pack ->
# congestion-price update}; the loop IS the search (no LAHC/ILS/recombine inside).
# See accord/accord_engine.py for the paradigm, falsification chain, and references.


def algorithm(prob_info, timelimit=60):
    """Entry point required by the evaluation server."""
    import os
    # Kernel gates are read at solver import time -> set BEFORE the import (setdefault
    # keeps external ablation overrides working). Same validated kernel flags as the
    # other solvers: supercover-mask scoring, numba kernels, multi-order best-of packer.
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    os.environ.setdefault("SOLVER_MULTIORDER", "1")
    # bay-parallel pack pool (spawn-probe guarded, serial fallback): frees 26-57% of
    # eval wall into loop iterations / the refine tail. T13 -3.1% / T14 -3.2% /
    # T20@180 -1.0%, no losses, no overruns (docs/accord_experiment_log.md 07-08).
    os.environ.setdefault("ACCORD_PAR", "1")
    import accord_engine as E
    try:
        return E.accord_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback: pure AABB build of the most-preferred
        # fitting bay (footprint-disjoint => never crane-trapped), exit ASAP.
        import sys
        _bridge = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge")
        if os.path.isdir(_bridge) and _bridge not in sys.path:
            sys.path.append(_bridge)
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_MULTIORDER"):
            os.environ.pop(_k, None)
        import solver
        return solver.build_solution(prob_info, solver.a_pref(prob_info))
