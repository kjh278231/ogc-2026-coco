"""WEAVE recombination + local-search operators on assignment vectors {block_id: bay_id}.

WEAVE is a fourth OGC 2026 solver (distinct from BRIDGE single-trajectory ILS/LAHC,
PRISM MIP-anchor-spectrum + independent LAHC + column-recombine, STOW packing-portfolio):
a co-evolving POPULATION that exchanges assignment structure DURING search via crossover
+ path-relinking, plus an ejection-chain local search (the k-opt generalisation of swap).

The dominant cost w3*Z3 + w2*Z2 is a PURE function of the assignment vector (a partition);
Z1 (tardiness) is packing-driven and ~0 at good assignments. So the levers are on the
partition. Reuses ONLY the validated per-bay packing + scoring primitives from BRIDGE
(imported as K); all adoption is guarded by K._bestof_obj (build-consistent, Pareto-safe).

Measured design decisions (docs/newsolver_experiment_design.md, .claude/scratch/_recomb_probe2):
  * pool diversity REQUIRES MIP anchors (else pref~=cap degenerate);
  * pair best x MAX-HAMMING partner (recombination material = assignment diversity, not
    parent quality): T18 cap x bal uniform-xover+polish = 70594 (-7.8%) beat equal-budget ILS;
  * uniform crossover > greedy (greedy chases Z3 -> bay crowding -> Z1 blow-up, T18 +25%).
"""
from __future__ import annotations
import os
import sys

# Reuse the BRIDGE kernel (append its dir so `import solver`/`packing`/`utils` resolve, exactly
# as PRISM does; appending keeps WEAVE's own modules ahead so nothing is shadowed).
_BRIDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
if _BRIDGE_DIR not in sys.path:
    sys.path.append(_BRIDGE_DIR)
import solver as K  # noqa: E402


# --------------------------------------------------------------------------- #
# crossover (assignment-vector level)
# --------------------------------------------------------------------------- #
def uniform_crossover(A, B, rng, p=0.5):
    """Each block inherits its bay from A (prob 1-p) or B (prob p). Always a valid
    assignment; over-loaded bays -> Z1, repaired by the subsequent polish."""
    return {i: (B[i] if rng.random() < p else A[i]) for i in A}


def greedy_crossover(prob, A, B):
    """Per block pick the parent bay with lower Z3 pref-loss (a 1-shot Z3 lower bound;
    tends to crowd preferred bays -> Z1; kept for ablation)."""
    blocks = prob["blocks"]
    ch = {}
    for i in A:
        p = blocks[i]["bay_preferences"]
        mx = max(p)
        ch[i] = A[i] if (mx - p[A[i]]) <= (mx - p[B[i]]) else B[i]
    return ch


# --------------------------------------------------------------------------- #
# path relinking (guided crossover: evaluate every waypoint S -> T)
# --------------------------------------------------------------------------- #
def path_relink(prob, S, T, cache, order="fixed", keep_endpoints=False):
    """Relink S toward T one differing block at a time, scoring each waypoint with
    K.total_obj; return the best INTERIOR waypoint (often unreachable by either endpoint
    or by 1-block relocation). order='fixed' O(diff); order='greedy' O(diff^2)."""
    diff = [i for i in S if S[i] != T[i]]
    cur = dict(S)
    cur_tot, _ = K.total_obj(prob, cur, cache)
    best, best_tot = (dict(cur), cur_tot) if keep_endpoints else (None, float("inf"))
    remaining = set(diff)
    while remaining:
        if order == "greedy":
            pick, pick_tot, pick_trial = None, float("inf"), None
            for i in sorted(remaining):
                trial = dict(cur)
                trial[i] = T[i]
                t, _ = K.total_obj(prob, trial, cache)
                if t < pick_tot - 1e-9 or (abs(t - pick_tot) <= 1e-9 and (pick is None or i < pick)):
                    pick, pick_tot, pick_trial = i, t, trial
            cur, cur_tot = pick_trial, pick_tot
            remaining.discard(pick)
        else:
            i = min(remaining)
            cur = dict(cur)
            cur[i] = T[i]
            cur_tot, _ = K.total_obj(prob, cur, cache)
            remaining.discard(i)
        if remaining or keep_endpoints:
            if cur_tot < best_tot - 1e-9:
                best, best_tot = dict(cur), cur_tot
    if best is None:
        best, best_tot = dict(T), K.total_obj(prob, T, cache)[0]
    return best, best_tot


# --------------------------------------------------------------------------- #
# ejection chain (k-opt generalisation of swap; feasibility-preserving)
# --------------------------------------------------------------------------- #
def _best_target(prob, cur, i, avoid):
    blocks = prob["blocks"]
    bays = prob["bays"]
    p = blocks[i]["bay_preferences"]
    for j in sorted(range(len(bays)), key=lambda j: -p[j]):
        if j != avoid and j != cur[i] and K.fits(blocks[i], bays[j]):
            return j
    return None


def ejection_chain(prob, assign, cache, seed_i, max_len=6):
    """One preference-guided ejection chain seeded at block seed_i: move it toward its
    preferred bay and PASS the resulting crowding along a chain of relocations until it
    dissipates in a bay with slack. Accept the closed chain only if the packed total_obj
    improves (Z1 kept ~0). Returns (new_assign, tot) or (None, None). <= max_len+1 evals."""
    cur = dict(assign)
    orig_tot, _ = K.total_obj(prob, cur, cache)
    moved = set()
    i = seed_i
    target = _best_target(prob, cur, i, None)
    if target is None:
        return None, None
    closed = False
    for _ in range(max_len):
        cur[i] = target
        moved.add(i)
        _, perbay = K.total_obj(prob, cur, cache)
        if perbay.get(target, 0) <= 1e-9:
            closed = True
            break
        cands = [j for j in cur if cur[j] == target and j not in moved]
        if not cands:
            return None, None
        j = min(cands, key=lambda j: prob["blocks"][j]["bay_preferences"][target])
        nt = _best_target(prob, cur, j, target)
        if nt is None:
            return None, None
        i, target = j, nt
    if not closed:
        return None, None
    tot, _ = K.total_obj(prob, cur, cache)
    return (cur, tot) if tot < orig_tot - 1e-9 else (None, None)


def ejection_chain_search(prob, assign, cache, deadline, max_len=6):
    """Drive ejection chains to a local optimum: seed at the off-preferred block with the
    largest Z3 gain potential, apply a chain, keep improvements (first-improve, re-scan
    after each accept). `deadline` = eval-count threshold (eval mode) or wall time."""
    blocks = prob["blocks"]
    m = len(prob["bays"])
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur = dict(assign)
    cur_tot, _ = K.total_obj(prob, cur, cache)
    improved = True
    while improved and K._within(deadline):
        improved = False
        seeds = sorted((i for i in cur if cur[i] != pref_bay[i]),
                       key=lambda i: -(blocks[i]["bay_preferences"][pref_bay[i]]
                                       - blocks[i]["bay_preferences"][cur[i]]))
        for i in seeds:
            if not K._within(deadline):
                break
            new, tot = ejection_chain(prob, cur, cache, i, max_len=max_len)
            if new is not None and tot < cur_tot - 1e-9:
                cur, cur_tot = new, tot
                improved = True
                break
    return cur, cur_tot


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def hamming(a, b):
    n = len(a)
    return sum(1 for i in a if a[i] != b[i]) / n if n else 0.0


def most_diverse(base, pool):
    """Return the (tag, assign, obj) in pool with max Hamming distance from `base`."""
    return max(pool, key=lambda e: hamming(base, e[1]))
