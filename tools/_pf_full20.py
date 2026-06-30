import json
def load(f, want=None):
    d={}
    for ln in open(f,encoding="utf-8"):
        ln=ln.strip()
        if ln.startswith("{"):
            try:
                o=json.loads(ln)
                if want is None or o["algo"]==want: d[o["inst"]]=o
            except: pass
    return d
# bridge: rest12 (paired, fresh) for the 12, plus hard-8 from the earlier valid run
Bp=load("tools/_prism_portf_rest12.txt","bridge")
Bh=load("tools/_prism_portf_ab180.txt","bridge")
B={**Bh,**Bp}
# prism: v3 (seed-diverse hard-8) + rest12 prism
Pv=load("tools/_prism_portf_ab180_v3.txt","prism")
Pp=load("tools/_prism_portf_rest12.txt","prism")
P={**Pv,**Pp}
insts=sorted(set(B)&set(P), key=lambda s:int(s[1:]))
bw=pw=tie=0; bt=pt=ot=0
print(f"{'inst':>5} {'BRIDGE':>9} {'PRISM':>9} {'d%':>7} {'win':>6}")
for i in insts:
    b=B[i]["obj"]; p=P[i]["obj"]
    if not(b and p): continue
    bt+=b; pt+=p; ot+=min(b,p); d=(p-b)/b*100
    w="PRISM" if p<b-0.5 else("bridge" if b<p-0.5 else "tie")
    if w=="PRISM":pw+=1
    elif w=="bridge":bw+=1
    else:tie+=1
    print(f"{i:>5} {b:>9} {p:>9} {d:>6.1f}% {w:>6}")
print("-"*42)
print(f"n={len(insts)} | PRISM {pw}W / bridge {bw}W / {tie}T")
print(f"agg bridge={bt} prism={pt} ({(pt-bt)/bt*100:+.1f}%) | oracle={ot} ({(ot-bt)/bt*100:+.1f}%)")
