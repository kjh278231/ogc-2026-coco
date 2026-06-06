"""Compare two place_initial snapshots and write a clean report to a file.

Usage:
    py -3.12 .claude/scratch/compare_init.py BEFORE.json AFTER.json OUT.txt
If AFTER is "-", only summarize BEFORE.
"""
import json
import sys
from collections import defaultdict


def load(p):
    d = json.load(open(p, encoding="utf-8"))
    return {k: v for k, v in d.items() if k != "_elapsed" and isinstance(v, dict)}


def summary(items):
    n = len(items)
    feas = sum(1 for v in items.values() if v.get("feasible"))
    errs = sum(1 for v in items.values() if "error" in v)
    sum_obj = sum(v["objective"] for v in items.values()
                  if v.get("feasible") and v.get("objective") is not None)
    tot_forced = sum(v.get("n_forced", 0) for v in items.values() if "n_forced" in v)
    max_place = max((v.get("place_s", 0) for v in items.values() if "place_s" in v),
                    default=0)
    byb = defaultdict(lambda: [0, 0.0])
    for v in items.values():
        if v.get("feasible"):
            byb[v["bays"]][0] += 1
            byb[v["bays"]][1] += v["objective"]
    return n, feas, errs, sum_obj, tot_forced, max_place, byb


def main():
    before_p, after_p, out_p = sys.argv[1], sys.argv[2], sys.argv[3]
    b = load(before_p)
    lines = []
    nb, fb, eb, sb, tfb, mpb, bybB = summary(b)
    lines.append(f"BEFORE  n={nb} feasible={fb} errors={eb} "
                 f"sum_obj={sb:.2f} tot_forced={tfb} max_place_s={mpb:.3f}")
    for k in sorted(bybB):
        lines.append(f"  before bays={k}: n={bybB[k][0]} sum_obj={bybB[k][1]:.2f}")

    if after_p != "-":
        a = load(after_p)
        na, fa, ea, sa, tfa, mpa, bybA = summary(a)
        lines.append(f"AFTER   n={na} feasible={fa} errors={ea} "
                     f"sum_obj={sa:.2f} tot_forced={tfa} max_place_s={mpa:.3f}")
        for k in sorted(bybA):
            lines.append(f"  after  bays={k}: n={bybA[k][0]} sum_obj={bybA[k][1]:.2f}")
        lines.append("")
        denom = sb if sb else 1.0
        lines.append(f"DELTA   feasible {fb}->{fa}  sum_obj {sb:.2f}->{sa:.2f} "
                     f"({sa - sb:+.2f}, {100 * (sa - sb) / denom:+.3f}%)  "
                     f"tot_forced {tfb}->{tfa}")
        lines.append("")
        lines.append("per-instance objective changes:")
        improved = regressed = same = newinfeas = 0
        for name in sorted(b):
            vb, va = b[name], a.get(name, {})
            ob = vb.get("objective")
            oa = va.get("objective")
            fbb = vb.get("feasible")
            faa = va.get("feasible")
            if fbb and not faa:
                newinfeas += 1
                lines.append(f"  !! {name}: feasible->INFEASIBLE (stage {va.get('stage')})")
                continue
            if ob is None or oa is None:
                continue
            d = oa - ob
            pct = 100 * d / ob if ob else 0.0
            if d < -0.01:
                improved += 1
            elif d > 0.01:
                regressed += 1
            else:
                same += 1
            tag = "IMPROVED" if d < -0.01 else ("regressed" if d > 0.01 else "same")
            lines.append(f"  {name}: {ob:.2f} -> {oa:.2f} ({d:+.2f}, {pct:+.3f}%) {tag}")
        lines.append("")
        lines.append(f"summary: improved={improved} regressed={regressed} "
                     f"unchanged={same} new_infeasible={newinfeas}")

    open(out_p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("wrote", out_p)


if __name__ == "__main__":
    main()
