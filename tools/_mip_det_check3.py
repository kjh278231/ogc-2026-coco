import os, sys, json, time
for k,v in (("SOLVER_MASK_SEARCH","1"),("SOLVER_MASK","1"),("SOLVER_NUMBA","1"),("SOLVER_MASK_PREPARE","1")):
    os.environ.setdefault(k,v)
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT,"prism"))
import prism_engine as P
import gurobipy as gp
from gurobipy import GRB
inst=sys.argv[1]
prob=json.load(open(os.path.join(ROOT,"train",f"{inst}.json"),encoding="utf-8"))
blocks,bays,w=prob["blocks"],prob["bays"],prob["weights"]
n,m=len(blocks),len(bays)
areas=[b["width"]*b["height"] for b in bays]; avg=sum(areas)/m; u=[avg/areas[j] for j in range(m)]
for lam in (1.0,8.0,64.0):
    md=gp.Model(env=P.K._grb_env()); md.Params.OutputFlag=0; md.Params.Threads=1; md.Params.Seed=0
    x=[[md.addVar(vtype=GRB.BINARY) for j in range(m)] for i in range(n)]
    for i in range(n):
        md.addConstr(gp.quicksum(x[i][j] for j in range(m))==1)
        for j in range(m):
            if not P.K.fits(blocks[i],bays[j]): md.addConstr(x[i][j]==0)
    load=[gp.quicksum(blocks[i]["workload"]*x[i][j] for i in range(n)) for j in range(m)]
    Mv=md.addVar(lb=0)
    for a in range(m):
        for b in range(m):
            if a!=b: md.addConstr(Mv>=u[a]*load[a]-u[b]*load[b])
    pref=gp.quicksum((max(blocks[i]["bay_preferences"])-blocks[i]["bay_preferences"][j])*x[i][j] for i in range(n) for j in range(m))
    md.setObjective(lam*w["w2"]*Mv+w["w3"]*pref,GRB.MINIMIZE); md.Params.TimeLimit=30
    t0=time.time(); md.optimize(); dt=time.time()-t0
    print(f"{inst} lam={lam:g}: status={md.Status} solcount={md.SolCount} obj={md.ObjVal if md.SolCount else None} gap={md.MIPGap if md.SolCount else None} t={dt:.2f}s")
