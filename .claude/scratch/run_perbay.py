"""Exp 2: does a CHEAP per-bay load metric (computed from the assignment alone)
predict which bay incurs tardiness?

For each bay under the baseline's actual assignment, compute assignment-intrinsic
spatial pressure (peak concurrent layer-0 area / bay area under the EARLIEST
schedule, i.e. every block resident on [R, R+P)) and pair it with the bay's
actual tardiness from the baseline solution.
"""
import sys, os, json, time, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline"))
import baseline_greedy, utils  # noqa


def shoelace(poly):
    a = 0.0; n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) * 0.5


def layer0_area(block):
    return shoelace(block["shape"][0]["layers"][0])


def analyze(path, timelimit):
    prob = json.load(open(path, encoding="utf-8"))
    blocks = prob["blocks"]; bays = prob["bays"]; m = len(bays)
    sol = baseline_greedy.greedyalgorithm(prob, timelimit)
    res = utils.check_feasibility(prob, sol)
    name = os.path.basename(path).replace(".json", "")
    if not res["feasible"]:
        print(f"# {name} INFEASIBLE"); return []

    # reconstruct assignment + actual exit
    bay_of = {}; exit_of = {}
    for t_str, ops in sol["operations"].items():
        t = int(t_str)
        for op in ops:
            if op["type"] == "ENTRY": bay_of[op["block_id"]] = op["bay_id"]
            else: exit_of[op["block_id"]] = t

    rows = []
    for j in range(m):
        ids = [b for b in range(len(blocks)) if bay_of.get(b) == j]
        bay_area = bays[j]["width"] * bays[j]["height"]
        # assignment-intrinsic peak concurrent area under EARLIEST schedule
        horizon = max((blocks[i]["release_time"] + blocks[i]["processing_time"]
                       for i in ids), default=0)
        demand = [0.0] * (horizon + 2)
        for i in ids:
            R = blocks[i]["release_time"]; P = blocks[i]["processing_time"]
            a = layer0_area(blocks[i])
            for t in range(R, R + P):
                demand[t] += a
        peak_util = (max(demand) / bay_area) if ids else 0.0
        tard = sum(max(0.0, exit_of[i] - blocks[i]["due_date"])
                   for i in ids if i in exit_of)
        rows.append((name, j, len(ids), bay_area, round(peak_util, 3), round(tard, 1)))
    return rows


if __name__ == "__main__":
    tl = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    pats = sys.argv[2:]
    paths = []
    for p in pats: paths += sorted(glob.glob(p))
    print(f"{'inst':9s} {'bay':3s} {'cnt':4s} {'area':6s} {'peakUtil':8s} {'tard':6s}")
    allrows = []
    for p in paths:
        for r in analyze(p, tl):
            allrows.append(r)
            print(f"{r[0]:9s} {r[1]:<3d} {r[2]:<4d} {r[3]:<6d} {r[4]:<8.3f} {r[5]:<6.1f}")
    # crude rank correlation peak_util vs tardiness
    if len(allrows) >= 3:
        import statistics
        pu = [r[4] for r in allrows]; td = [r[5] for r in allrows]
        def rank(xs):
            order = sorted(range(len(xs)), key=lambda i: xs[i])
            rk = [0] * len(xs)
            for pos, i in enumerate(order): rk[i] = pos
            return rk
        rp, rt = rank(pu), rank(td)
        n = len(pu)
        d2 = sum((rp[i] - rt[i]) ** 2 for i in range(n))
        rho = 1 - 6 * d2 / (n * (n * n - 1))
        tardy = [r for r in allrows if r[5] > 0]
        notardy = [r for r in allrows if r[5] == 0]
        print(f"\n# n_bays={n}  Spearman(peakUtil, tardiness) rho = {rho:.3f}")
        if tardy and notardy:
            print(f"# peakUtil  tardy bays: mean={statistics.mean(r[4] for r in tardy):.3f} "
                  f"(min={min(r[4] for r in tardy):.3f})")
            print(f"# peakUtil  zero-tard  : mean={statistics.mean(r[4] for r in notardy):.3f} "
                  f"(max={max(r[4] for r in notardy):.3f})")
