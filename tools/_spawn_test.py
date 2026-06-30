import os, sys
if __name__ == "__main__":
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(ROOT, "prism"))
    import portfolio
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    try:
        with ctx.Pool(1) as p:
            r = p.apply_async(portfolio._probe)
            print("PROBE OK, result =", r.get(timeout=25))
    except Exception as e:
        print("SPAWN FAILED:", repr(e))
