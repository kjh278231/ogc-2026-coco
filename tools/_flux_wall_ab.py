"""Wall-clock A/B of FLUX vs PRISM deployed portfolios, back-to-back on one instance (measured
close in time to control absolute drift). Runs each engine in its OWN subprocess via
_prism_portf_ab.py (module-name collision safe: flux/ and prism/ both have myalgorithm+portfolio).
Single Gurobi license -> strictly sequential (never concurrent).

    python tools/_flux_wall_ab.py <inst> <T> [prism|flux first]
"""
import os, sys, json, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
RUNNER = os.path.join(ROOT, "tools", "_prism_portf_ab.py")


def one(algo, inst, T):
    p = subprocess.run([PY, RUNNER, algo, inst, str(T)], capture_output=True, text=True,
                       cwd=ROOT, timeout=T + 90)
    out = p.stdout.strip().splitlines()
    for ln in out:
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                return json.loads(ln)
            except Exception:
                pass
    sys.stderr.write(f"[{algo} {inst} T={T}] no json. stderr tail:\n" + (p.stderr[-500:] or "") + "\n")
    return None


def main():
    inst, T = sys.argv[1], float(sys.argv[2])
    order = sys.argv[3].split(",") if len(sys.argv) > 3 else ["prism", "flux"]
    res = {}
    for algo in order:
        r = one(algo, inst, T)
        res[algo] = r
        if r:
            print(f"{algo:>5} {inst} T={T:.0f}: feasible={r['feasible']} obj={r['obj']} "
                  f"obj123={r.get('obj123')} wall={r['wall_s']}s", flush=True)
    if res.get("prism") and res.get("flux") and res["prism"]["obj"] and res["flux"]["obj"]:
        op, of = res["prism"]["obj"], res["flux"]["obj"]
        d = 100 * (of - op) / op
        tag = "FLUX" if d < -0.05 else ("PRISM" if d > 0.05 else "tie")
        print(f"==> {inst} T={T:.0f}: PRISM {op} vs FLUX {of}  ({d:+.1f}% {tag})", flush=True)


if __name__ == "__main__":
    main()
