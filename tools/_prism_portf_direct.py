import os, sys, json, time
if __name__ == "__main__":
    for k,v in (("SOLVER_MASK_SEARCH","1"),("SOLVER_MASK","1"),("SOLVER_NUMBA","1"),("SOLVER_MASK_PREPARE","1")):
        os.environ.setdefault(k,v)
    inst = sys.argv[1] if len(sys.argv)>1 else "T20"
    T = float(sys.argv[2]) if len(sys.argv)>2 else 60.0
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(ROOT, "prism"))
    import prism_engine, portfolio
    prob = json.load(open(os.path.join(ROOT,"train",f"{inst}.json"),encoding="utf-8"))
    t0=time.time()
    sol = portfolio.portfolio_solve(prob, T)
    wall=time.time()-t0
    print("LAST:", json.dumps(portfolio.LAST))
    print("wall:", round(wall,1))
