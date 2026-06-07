"""
Experiment 0 — problem-condition characterization (NO solver, NO shapely).

For each instance we answer three questions that decide whether the
spatial/assignment decomposition can be driven by a cheap, verifiable bound:

  Q1  Is the temporal-only tardiness lower bound ~0?
        LB_temporal = sum_i max(0, R_i + P_i - D_i)
      If ~0, ALL tardiness is spatially/crane induced.

  Q2  Under the *earliest* schedule (every block resident on [R_i, R_i+P_i)),
      does layer-0 area demand exceed capacity?
        Non-overlap within a bay at one level => sum of resident layer-0 areas
        <= total bay area is a HARD constraint. So overflow at any time means
        at least one block MUST be delayed: a valid, assignment-free spatial
        tardiness lower bound.

  Q3  Block-area / bay-area ratios: how many blocks coexist at one level.
"""
import json, math, glob, os, statistics

def shoelace(poly):
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) * 0.5

def layer0_area(block):
    # area is rotation/reflection invariant; orientation 0 layer 0 footprint
    layers = block["shape"][0]["layers"]
    return shoelace(layers[0])

def analyze(path):
    inst = json.load(open(path, encoding="utf-8"))
    bays = inst["bays"]; blocks = inst["blocks"]
    n, m = len(blocks), len(bays)
    cap_total = sum(b["width"] * b["height"] for b in bays)
    cap_avg = cap_total / m

    R = [b["release_time"] for b in blocks]
    D = [b["due_date"] for b in blocks]
    P = [b["processing_time"] for b in blocks]
    slack = [D[i] - R[i] - P[i] for i in range(n)]
    lb_temporal = sum(max(0, R[i] + P[i] - D[i]) for i in range(n))

    areas = [layer0_area(b) for b in blocks]

    # Earliest schedule: block i resident on [R_i, R_i + P_i)
    horizon = max(R[i] + P[i] for i in range(n))
    demand = [0.0] * (horizon + 1)
    for i in range(n):
        for t in range(R[i], R[i] + P[i]):
            demand[t] += areas[i]
    peak = max(demand)
    overflow_area_days = sum(max(0.0, demand[t] - cap_total) for t in range(horizon + 1))
    overflow_steps = sum(1 for t in range(horizon + 1) if demand[t] > cap_total + 1e-9)

    return {
        "name": inst.get("name", os.path.basename(path)),
        "n": n, "m": m, "horizon": horizon,
        "cap_total": cap_total, "cap_avg": cap_avg,
        "slack_min": min(slack), "slack_med": statistics.median(slack), "slack_max": max(slack),
        "frac_zero_slack": sum(1 for s in slack if s == 0) / n,
        "lb_temporal": lb_temporal,
        "area_mean": statistics.mean(areas), "area_max": max(areas),
        "area_frac_of_bayavg": statistics.mean(areas) / cap_avg,
        "peak_demand": peak, "peak_util_vs_total": peak / cap_total,
        "overflow_area_days": overflow_area_days, "overflow_steps": overflow_steps,
    }

def main():
    paths = sorted(glob.glob("train/*.json"), key=lambda p: int(''.join(c for c in os.path.basename(p) if c.isdigit())))
    rows = [analyze(p) for p in paths]
    cols = ["name", "n", "m", "horizon", "lb_temporal",
            "slack_min", "slack_med", "slack_max", "frac_zero_slack",
            "area_frac_of_bayavg", "peak_util_vs_total", "overflow_steps", "overflow_area_days"]
    # print aligned
    w = {c: max(len(c), max(len(f"{r[c]:.3g}" if isinstance(r[c], float) else str(r[c])) for r in rows)) for c in cols}
    print(" | ".join(c.ljust(w[c]) for c in cols))
    print("-+-".join("-" * w[c] for c in cols))
    for r in rows:
        cells = []
        for c in cols:
            v = r[c]
            s = f"{v:.3g}" if isinstance(v, float) else str(v)
            cells.append(s.ljust(w[c]))
        print(" | ".join(cells))

if __name__ == "__main__":
    main()
