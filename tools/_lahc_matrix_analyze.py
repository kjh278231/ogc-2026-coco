"""Read tools/_lahc_matrix.txt (RESULT <inst> <prof> <obj> ...) and compare the
current portfolio worker set against candidate sets that swap in the diverse worker.

Portfolio best-of over workers = min worker obj per instance (the anchor's recombine and
the master union-recombine are common to every candidate set, so they cancel in the
comparison -> NORECOMB worker objs are a fair proxy for the master's best-of pick)."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
rows = {}
for ln in open(os.path.join(HERE, "_lahc_matrix.txt"), encoding="utf-8"):
    p = ln.split()
    if len(p) >= 4 and p[0] == "RESULT":
        rows.setdefault(p[1], {})[p[2]] = float(p[3])

insts = sorted(rows, key=lambda s: int(s[1:]))

SETS = {
    "current   (anc,grd,l30,i01)": ["anc", "grd", "l30", "i01"],
    "swap i01->div               ": ["anc", "grd", "l30", "div"],
    "swap i01->div01             ": ["anc", "grd", "l30", "div01"],
    "swap l30->div               ": ["anc", "grd", "div", "i01"],
    "swap grd->div               ": ["anc", "div", "l30", "i01"],
}

def bestof(d, keys):
    vals = [d[k] for k in keys if k in d]
    return min(vals) if vals else float("nan")

# per-profile table
profs = ["anc", "grd", "l30", "i01", "div", "div01"]
print("inst   " + "".join("%12s" % p for p in profs))
for it in insts:
    print("%-6s " % it + "".join("%12.0f" % rows[it].get(p, float('nan')) for p in profs))

print()
base = "current   (anc,grd,l30,i01)"
base_vals = {it: bestof(rows[it], SETS[base]) for it in insts}
print("%-30s %10s" % ("set", "sum"), "  per-instance delta vs current")
for name, keys in SETS.items():
    tot = 0.0
    deltas = []
    for it in insts:
        b = bestof(rows[it], keys)
        tot += b
        d = (b - base_vals[it]) / base_vals[it] * 100 if base_vals[it] else 0.0
        deltas.append("%s%+.1f%%" % (it, d))
    print("%-30s %10.0f" % (name, tot), " " + " ".join(deltas))
