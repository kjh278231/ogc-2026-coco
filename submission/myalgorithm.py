# myalgorithm.py -- OGC 2026 submission entry point (BRIDGE solver).
#
# BRIDGE = Bay-decomposed Recombining Iterated-search with Disjoint-footprint
#          admission and Guarded Escalating-polygon packing.
#
# Framework: assignment search scored by a per-bay footprint-disjoint admission
# packer (crane extraction is free by construction since no two co-resident
# blocks share a footprint, affordable because bay area utilisation is low).
# Time-managed to respect `timelimit`; always returns a feasible solution.
# See docs/technical_report.md and docs/methodology.md.


def algorithm(prob_info, timelimit=60):
    """
    Entry point required by the evaluation server.
    `prob_info`: instance dict (bays, blocks, weights).
    `timelimit`: wall-clock budget in seconds.
    Returns a solution dict {"operations": {...}}.
    """
    import solver
    try:
        return solver.framework_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback: most-preferred-bay assignment,
        # footprint-disjoint placement (never crane-trapped), exit ASAP.
        return solver.build_solution(prob_info, solver.a_pref(prob_info))
