import os, sys, json, hashlib, time
for k,v in (("SOLVER_MASK_SEARCH","1"),("SOLVER_MASK","1"),("SOLVER_NUMBA","1"),("SOLVER_MASK_PREPARE","1")):
    os.environ.setdefault(k,v)
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT,"prism"))
import prism_engine as P
inst=sys.argv[1]
prob=json.load(open(os.path.join(ROOT,"train",f"{inst}.json"),encoding="utf-8"))
def h(a): return hashlib.md5(json.dumps(sorted(a.items())).encode()).hexdigest()[:10] if a else None
for lam in (1.0,8.0,64.0):
    t0=time.time(); a=P.mip_anchor(prob,lam,4.0); dt=time.time()-t0
    z2,z3=P.K.obj23(prob,a)
    print(f"{inst} lam={lam:g}: hash={h(a)} z2={z2:.0f} z3={z3:.0f} t={dt:.2f}s")
