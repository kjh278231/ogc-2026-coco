# myalgorithm.py -- OGC 2026 entry point for STOW (fourth algorithm, placement-centric).
#
# STOW's thesis (see docs/stow_experiment_log.md, memory/placement-lever-diagnosis.md):
# every existing solver (BRIDGE / PRISM / ALNS) shares ONE fixed per-bay packer (EDD order,
# bottom-left, union-disjoint) and only ever diversifies the ASSIGNMENT search. But tardiness
# Z1 is 100% contention (temporal floor = 0), and the placement ORDER is a real lever: a
# best-of over {EDD, release, least-slack, area-desc} cuts a_pref Z1 -24% (release alone -19%).
# As a SEARCH packer this multi-order best-of is a strong but instance-split lever (big wins
# T1 -91% / T9 -52% / T11 -48% / T13 -36% / T6 -64% at equal eval count, but its slower
# per-eval throughput can drift a wall-bounded single run). So STOW makes the PACKING POLICY
# the diversity axis of a parallel best-of portfolio -- some workers search against the
# multi-order packer, some against EDD -- and the master scores every candidate with the
# multi-order build. best-of(EDD-search, multiorder-search) was pure upside on the wall A/B
# (6 wins, 0 regressions). This is orthogonal to BRIDGE (seed diversity) and PRISM (MIP-anchor
# diversity); it reuses BRIDGE's validated kernel + portfolio harness, swapping only PROFILES.

import os
import sys

_BRIDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE_DIR not in sys.path:
    sys.path.append(_BRIDGE_DIR)   # append: STOW's own modules (if any) win; bridge kernel falls through

# Packing-policy-diverse worker profiles (4 cores). Worker 0 (ANCHOR) recombines during the
# worker window (single-use Gurobi license -> exactly one recombiner); the other 3 are NORECOMB
# diversity and feed the master union-recombine. The diversity axis is SOLVER_MULTIORDER (the
# new packing lever) crossed with BRIDGE's restart diversity (LAHC DIVERSE + per-worker seed):
#   w0  multi-order + recombine                (the lever, with its Pareto floor)
#   w1  EDD         + NORECOMB                  (guards multi-order search drift, e.g. T18; distinct pool)
#   w2  multi-order + DIVERSE restart           (multi-order trajectory diversity; trap escape)
#   w3  EDD         + DIVERSE restart           (BRIDGE's validated trap-escape worker, e.g. T1)
STOW_PROFILES = [
    {"SOLVER_SEED": "20265", "SOLVER_MULTIORDER": "1", "SOLVER_NORECOMB": "0",
     "SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1"},
    {"SOLVER_SEED": "12346", "SOLVER_MULTIORDER": "0",
     "SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1"},
    {"SOLVER_SEED": "28184", "SOLVER_MULTIORDER": "1",
     "SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1", "SOLVER_LAHC_DIVERSE": "1",
     "SOLVER_LAHC_INIT_FRAC": "0.1"},
    {"SOLVER_SEED": "36103", "SOLVER_MULTIORDER": "0",
     "SOLVER_LAHC": "1", "SOLVER_LAHC_L": "1", "SOLVER_LAHC_DIVERSE": "1",
     "SOLVER_LAHC_INIT_FRAC": "0.1"},
]


def algorithm(prob_info, timelimit=60):
    """Entry point required by the evaluation server. Returns {"operations": {...}}."""
    # Same import-time stack as BRIDGE, plus SOLVER_MULTIORDER on the MASTER so the final
    # build / candidate scoring uses the best-of-orders packing (Pareto-better Z1 for every
    # candidate). Workers set their own SOLVER_MULTIORDER per profile (spawn = fresh import).
    os.environ.setdefault("SOLVER_MASK_SEARCH", "1")
    os.environ.setdefault("SOLVER_MASK", "1")
    os.environ.setdefault("SOLVER_ADAPTIVE_RESERVE", "1")
    os.environ.setdefault("SOLVER_NUMBA", "1")
    os.environ.setdefault("SOLVER_UNIFIED_ILS", "1")
    os.environ.setdefault("SOLVER_UNIFIED_INIT_FRAC", "0.6")
    os.environ.setdefault("SOLVER_UNIFIED_INIT_CAP", "45")
    os.environ.setdefault("SOLVER_MASK_PREPARE", "1")
    os.environ.setdefault("SOLVER_LAHC", "1")
    os.environ.setdefault("SOLVER_LAHC_L", "1")
    os.environ.setdefault("SOLVER_IDLE_ILS", "1")
    os.environ.setdefault("SOLVER_MIP_REPAIR", "1")
    # STOW's lever: multi-order best-of packing for the master build + the single-process path.
    os.environ.setdefault("SOLVER_MULTIORDER", "1")
    # Portfolio gate: the parallel best-of is pure upside (it guards the multi-order regressions),
    # so capture it from a low floor like PRISM's validated 45 (P1/P2 too). T<45 -> single-process.
    os.environ.setdefault("SOLVER_PORTFOLIO", "1")
    _min_t = float(os.environ.get("SOLVER_PORTFOLIO_MIN_T", "45"))
    import solver
    try:
        if os.environ.get("SOLVER_PORTFOLIO") not in (None, "", "0") and timelimit >= _min_t:
            import portfolio          # bridge's validated harness; we only swap the profile set
            portfolio.PROFILES = STOW_PROFILES
            return portfolio.portfolio_solve(prob_info, timelimit)
        return solver.framework_solve(prob_info, timelimit)
    except Exception:
        # last-resort guaranteed-feasible fallback (pure AABB, most-preferred-bay).
        os.environ["SOLVER_NOPOLY"] = "1"
        for _k in ("SOLVER_MASK", "SOLVER_MASK_SEARCH", "SOLVER_MULTIORDER"):
            os.environ.pop(_k, None)
        return solver.build_solution(prob_info, solver.a_pref(prob_info))
