import json, sys
def load(f):
    d={}
    for ln in open(f,encoding="utf-8"):
        ln=ln.strip()
        if ln.startswith("{"):
            try: o=json.loads(ln); d[o["inst"]]=o
            except: pass
    return d
B=load("tools/_prism_portf_ab180.txt")      # has valid bridge entries
P=load(sys.argv[1])                           # prism v2
insts=sorted(set(P)&set(i for i in B if B[i]["algo"]=="bridge"), key=lambda s:int(s[1:]))
bw=pw=tie=0; bt=pt=ot=0
print(f"{'inst':>5} {'BRIDGE-pf':>10} {'PRISM-pf':>10} {'d%':>7} {'win':>6}  PRISM[z1z2z3]")
for i in insts:
    b=B[i]["obj"]; p=P[i]["obj"]
    if b is None or p is None: continue
    bt+=b; pt+=p; ot+=min(b,p)
    d=(p-b)/b*100
    w="PRISM" if p<b-0.5 else("bridge" if b<p-0.5 else "tie")
    if w=="PRISM":pw+=1
    elif w=="bridge":bw+=1
    else:tie+=1
    print(f"{i:>5} {b:>10} {p:>10} {d:>6.1f}% {w:>6}  {P[i]['obj123']}")
print("-"*60)
print(f"PRISM {pw}W / bridge {bw}W / {tie}T | agg bridge={bt} prism={pt} ({(pt-bt)/bt*100:+.1f}%) | oracle={ot} ({(ot-bt)/bt*100:+.1f}%)")
