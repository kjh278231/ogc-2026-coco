import os, sys, json, hashlib
for k,v in (("SOLVER_MASK_SEARCH","1"),("SOLVER_MASK","1"),("SOLVER_NUMBA","1"),("SOLVER_MASK_PREPARE","1")):
    os.environ.setdefault(k,v)
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT,"prism"))
import prism_engine as P
inst=sys.argv[1]
prob=json.load(open(os.path.join(ROOT,"train",f"{inst}.json"),encoding="utf-8"))
def h(a): return hashlib.md5(json.dumps(sorted(a.items())).encode()).hexdigest()[:10] if a else None
for trial in range(3):
    a=P.mip_anchor(prob,1.0,4.0)
    z2,z3=P.K.obj23(prob,a)
    print(f"{inst} trial{trial}: hash={h(a)} z2={z2:.0f} z3={z3:.0f}")
