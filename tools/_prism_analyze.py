import json, sys
rows = {}
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line.startswith("{"): continue
    try: d = json.loads(line)
    except Exception: continue
    rows.setdefault(d["inst"], {})[d["algo"]] = d
insts = sorted(rows, key=lambda s: int(s[1:]))
bw = pw = ties = 0
btot = ptot = otot = 0
print(f"{'inst':>5} {'bridge':>10} {'prism':>10} {'delta%':>8}  {'oracle':>10}  winner")
for inst in insts:
    r = rows[inst]
    if "bridge" not in r or "prism" not in r: 
        print(f"{inst:>5}  (incomplete: {list(r)})"); continue
    b, p = r["bridge"]["obj"], r["prism"]["obj"]
    o = min(b, p); btot += b; ptot += p; otot += o
    d = (p - b) / b * 100 if b else 0
    win = "PRISM" if p < b - 0.5 else ("bridge" if b < p - 0.5 else "tie")
    if win == "PRISM": pw += 1
    elif win == "bridge": bw += 1
    else: ties += 1
    print(f"{inst:>5} {b:>10} {p:>10} {d:>7.1f}%  {o:>10}  {win}  P{r['prism']['z1z2z3']} B{r['bridge']['z1z2z3']}")
print("-"*70)
print(f"PRISM wins={pw} bridge wins={bw} ties={ties}")
print(f"aggregate: bridge={btot} prism={ptot} ({(ptot-btot)/btot*100:+.1f}%)")
print(f"oracle best-of={otot} vs bridge={btot} ({(otot-btot)/btot*100:+.1f}%)")
