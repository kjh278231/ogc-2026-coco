"""Assignment lever: evaluate the FULL objective (w1*obj1+w2*obj2+w3*obj3) of the
disjoint-packing framework under several assignment strategies, vs baseline.

obj2/obj3 are instant functions of the assignment; obj1 needs the per-bay packing
sim (fast, crane-free). So eval_assignment = instant obj2/obj3 + m packing sims.
Then a light local search (move tardy blocks across bays) on top of the best.
"""
import sys, os, json, math, time
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline"))
sys.path.insert(0, HERE)
import baseline_greedy, utils
from prototype import solve_bay, extract_tardiness, orient_bbox


def fits(bd, bay):
    # integer-reference feasibility (matches the instance generator): an integer
    # (x,y) placement must keep the block fully inside the bay.
    for o in range(len(bd["shape"])):
        mnx, mny, mxx, mxy = orient_bbox(bd, o)
        if (math.ceil(max(0.0, -mnx)) + mxx <= bay["width"]
                and math.ceil(max(0.0, -mny)) + mxy <= bay["height"]):
            return True
    return False


def obj23(prob, assign):
    blocks, bays = prob["blocks"], prob["bays"]; m = len(bays)
    loads = [0.0] * m
    obj3 = 0.0
    for i, j in assign.items():
        loads[j] += blocks[i]["workload"]
        obj3 += max(blocks[i]["bay_preferences"]) - blocks[i]["bay_preferences"][j]
    areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
    u = [avg / a for a in areas]
    obj2 = math.floor(max((abs(u[a] * loads[a] - u[b] * loads[b])
                           for a in range(m) for b in range(m) if a != b), default=0.0))
    return obj2, obj3, loads


def eval_obj1(prob, assign, cache):
    m = len(prob["bays"]); obj1 = 0.0; perbay = {}
    for j in range(m):
        ids = tuple(sorted(i for i, a in assign.items() if a == j))
        if not ids:
            perbay[j] = 0.0; continue
        if ids in cache:
            perbay[j] = cache[ids]
        else:
            placed = solve_bay(prob, j, list(ids))
            T, _ = extract_tardiness(prob, j, placed)
            cache[ids] = T; perbay[j] = T
        obj1 += perbay[j]
    return obj1, perbay


def total_obj(prob, assign, cache):
    obj1, perbay = eval_obj1(prob, assign, cache)
    obj2, obj3, _ = obj23(prob, assign)
    w = prob["weights"]
    tot = w["w1"] * obj1 + w["w2"] * obj2 + w["w3"] * obj3
    return tot, obj1, obj2, obj3, perbay


# ---- assignment heuristics ----
def a_pref(prob):
    blocks, bays = prob["blocks"], prob["bays"]
    asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        asg[i] = next((j for j in order if fits(b, bays[j])), order[0])
    return asg


def a_balanced_load(prob):
    """Largest-workload-first to currently lightest (u*load) feasible bay."""
    blocks, bays = prob["blocks"], prob["bays"]; m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
    u = [avg / a for a in areas]
    loads = [0.0] * m; asg = {}
    for i in sorted(range(len(blocks)), key=lambda i: -blocks[i]["workload"]):
        b = blocks[i]
        cand = [j for j in range(m) if fits(b, bays[j])] or list(range(m))
        j = min(cand, key=lambda j: u[j] * (loads[j] + b["workload"]))
        asg[i] = j; loads[j] += b["workload"]
    return asg


def a_pref_capped(prob, cap_factor=1.15):
    """Preference, but cap per-bay block COUNT to balance admission load."""
    blocks, bays = prob["blocks"], prob["bays"]; m = len(bays)
    cap = math.ceil(len(blocks) / m * cap_factor)
    cnt = [0] * m; asg = {}
    for i, b in enumerate(blocks):
        order = sorted(range(len(bays)), key=lambda j: -b["bay_preferences"][j])
        order = [j for j in order if fits(b, bays[j])] or order
        j = next((j for j in order if cnt[j] < cap), order[0])
        asg[i] = j; cnt[j] += 1
    return asg


def local_search(prob, assign, cache, budget_s):
    m = len(prob["bays"]); blocks = prob["blocks"]
    best = dict(assign)
    best_tot, o1, o2, o3, perbay = total_obj(prob, best, cache)
    t0 = time.time()
    improved = True
    while improved and time.time() - t0 < budget_s:
        improved = False
        # focus on blocks in bays that currently carry tardiness
        tardy_bays = [j for j in range(m) if perbay.get(j, 0) > 0]
        movers = [i for i in best if best[i] in tardy_bays]
        for i in movers:
            if time.time() - t0 >= budget_s:
                break
            cur = best[i]
            for j in range(m):
                if j == cur or not fits(blocks[i], prob["bays"][j]):
                    continue
                trial = dict(best); trial[i] = j
                tot, *_rest = total_obj(prob, trial, cache)
                if tot < best_tot - 1e-9:
                    best, best_tot = trial, tot
                    _, o1, o2, o3, perbay = total_obj(prob, best, cache)
                    improved = True
                    break
    return best, best_tot


def improved_search(prob, cache, budget_s):
    """Two-phase hill climb from the best heuristic seed. Phase 1 (first half of
    budget): cheap high-value movers only — blocks in tardy bays (Z1) and in the
    max-(u*load) bay (Z2). Phase 2 (rest): also move blocks off their preferred bay
    (Z3), tried preference-first. Phase 1 reproduces the proven focused search, so
    quality never regresses below it; phase 2 only adds accepted (improving) moves."""
    t0 = time.time()
    blocks, bays = prob["blocks"], prob["bays"]; m = len(bays)
    areas = [b["width"] * b["height"] for b in bays]; avg = sum(areas) / m
    u = [avg / a for a in areas]
    pref_bay = {i: max(range(m), key=lambda j: blocks[i]["bay_preferences"][j])
                for i in range(len(blocks))}
    cur, cur_tot = None, float("inf")
    for fn in (a_pref, a_balanced_load, a_pref_capped):
        a = fn(prob); tot, *_ = total_obj(prob, a, cache)
        if tot < cur_tot:
            cur, cur_tot = dict(a), tot
    _, o1, o2, o3, perbay = total_obj(prob, cur, cache)

    def hillclimb(include_offpref, deadline):
        nonlocal cur, cur_tot, o1, o2, o3, perbay
        improved = True
        while improved and time.time() < deadline:
            improved = False
            loads = [0.0] * m
            for i, j in cur.items():
                loads[j] += blocks[i]["workload"]
            maxload = max(range(m), key=lambda j: u[j] * loads[j])
            tardy = {j for j in range(m) if perbay.get(j, 0) > 0}
            movers = [i for i in cur if cur[i] in tardy or cur[i] == maxload
                      or (include_offpref and cur[i] != pref_bay[i])]
            for i in movers:
                if time.time() >= deadline:
                    break
                targets = sorted((j for j in range(m)
                                  if j != cur[i] and fits(blocks[i], bays[j])),
                                 key=lambda j: -blocks[i]["bay_preferences"][j])
                for j in targets:
                    trial = dict(cur); trial[i] = j
                    tot, *_r = total_obj(prob, trial, cache)
                    if tot < cur_tot - 1e-9:
                        cur, cur_tot = trial, tot
                        _, o1, o2, o3, perbay = total_obj(prob, cur, cache)
                        improved = True
                        break

    hillclimb(False, t0 + budget_s * 0.5)
    hillclimb(True, t0 + budget_s)
    return cur, cur_tot


def run(path, baseline_tl=60):
    prob = json.load(open(path, encoding="utf-8"))
    name = os.path.basename(path).replace(".json", "")
    w = prob["weights"]
    print(f"\n{name}  weights w1={w['w1']} w2={w['w2']} w3={w['w3']}")
    # baseline reference
    sol = baseline_greedy.greedyalgorithm(prob, baseline_tl)
    res = utils.check_feasibility(prob, sol)
    if res["feasible"]:
        print(f"  baseline      : total={res['objective']:.0f}  "
              f"obj1={res['obj1']:.0f} obj2={res['obj2']:.0f} obj3={res['obj3']:.0f}")
        base_tot = res["objective"]
    else:
        print(f"  baseline      : INFEASIBLE"); base_tot = None
    cache = {}
    best_seed = None; best_seed_tot = float("inf")
    for label, fn in [("pref", a_pref), ("balanced_load", a_balanced_load),
                      ("pref_capped", a_pref_capped)]:
        asg = fn(prob)
        tot, o1, o2, o3, _ = total_obj(prob, asg, cache)
        if tot < best_seed_tot:
            best_seed_tot, best_seed = tot, asg
        mark = " <<" if base_tot is not None and tot < base_tot else ""
        print(f"  {label:13s} : total={tot:.0f}  obj1={o1:.0f} obj2={o2:.0f} obj3={o3:.0f}{mark}")
    # local search from the BEST heuristic seed
    t0 = time.time()
    best, best_tot = local_search(prob, best_seed, cache, budget_s=30)
    tot, o1, o2, o3, _ = total_obj(prob, best, cache)
    mark = " <<" if base_tot is not None and tot < base_tot else ""
    print(f"  +local_search : total={tot:.0f}  obj1={o1:.0f} obj2={o2:.0f} obj3={o3:.0f}{mark}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        run(p)
