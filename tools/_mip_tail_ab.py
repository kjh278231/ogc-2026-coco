"""Tail-placement MIP-repair A/B driver.

Runs {control, mip} x {winners, losers} at T=60 with bounded concurrency, then prints a
per-instance control-vs-mip % table. Each child is a fresh process (one instance / one cfg)
launched via _mip_ab.py so module-level caches never cross instances.
Usage: python tools/_mip_tail_ab.py
"""
import os, sys, json, subprocess, concurrent.futures as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
INSTS = (sys.argv[1].split(",") if len(sys.argv) > 1
         else ["prob_11", "prob_13", "prob_20", "prob_37", "prob_26", "prob_40"])
T = "60"
MAXW = 1  # fully sequential: each run gets the whole machine, incl. the parallel MIP sidecar
          # -> clean, comparable walls (any concurrency starves the single-core build probe)


def run(inst, cfg):
    p = subprocess.run([PY, os.path.join(ROOT, "tools", "_mip_ab.py"), inst, T, cfg],
                       capture_output=True, text=True, cwd=ROOT)
    for line in p.stdout.splitlines():
        if line.startswith("RESULT "):
            body = line[len("RESULT "):]
            # _mip_ab.py appends "  <<< PROBLEM" on infeasible/overrun -> strip before parsing.
            cut = body.find("} ")
            if cut != -1:
                body = body[:cut + 1]
            try:
                return json.loads(body)
            except Exception:
                return {"inst": inst, "cfg": cfg, "feasible": False, "obj": None,
                        "wall": None, "over": True, "raw": line[:200]}
    return {"inst": inst, "cfg": cfg, "err": p.stdout[-300:] + p.stderr[-300:]}


if __name__ == "__main__":
    jobs = [(i, c) for i in INSTS for c in ("control", "mip")]
    res = {}
    with cf.ProcessPoolExecutor(max_workers=MAXW) as ex:
        futs = {ex.submit(run, i, c): (i, c) for (i, c) in jobs}
        for f in cf.as_completed(futs):
            r = f.result()
            res[(r.get("inst"), r.get("cfg"))] = r
            print("DONE", r, flush=True)
    print("\n==== TAIL MIP-REPAIR A/B (T=60) ====")
    print(f"{'inst':10s} {'control':>12s} {'mip':>12s} {'mip_vs_ctrl%':>12s} "
          f"{'ctrl_wall':>9s} {'mip_wall':>9s} {'over':>6s}")
    for i in INSTS:
        c = res.get((i, "control"), {})
        m = res.get((i, "mip"), {})
        co, mo = c.get("obj"), m.get("obj")
        pct = (mo - co) / co * 100 if (co and mo) else float("nan")
        over = bool(c.get("over") or m.get("over"))
        print(f"{i:10s} {co!s:>12s} {mo!s:>12s} {pct:>11.2f}% "
              f"{c.get('wall')!s:>9s} {m.get('wall')!s:>9s} {over!s:>6s}")
