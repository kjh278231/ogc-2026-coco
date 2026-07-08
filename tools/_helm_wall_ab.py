"""Wall-clock A/B of HELM vs PRISM deployed portfolios, back-to-back on one instance
(measured close in time to control absolute drift). Each engine in its OWN subprocess via
_prism_portf_ab.py. Single Gurobi license -> strictly sequential.

    python tools/_helm_wall_ab.py <inst> <T> [order e.g. prism,helm]
"""
import os, sys, json, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
RUNNER = os.path.join(ROOT, "tools", "_prism_portf_ab.py")


def one(algo, inst, T):
    p = subprocess.run([PY, RUNNER, algo, inst, str(T)], capture_output=True, text=True,
                       cwd=ROOT, timeout=T + 120)
    for ln in p.stdout.strip().splitlines():
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
    order = sys.argv[3].split(",") if len(sys.argv) > 3 else ["prism", "helm"]
    res = {}
    for algo in order:
        r = one(algo, inst, T)
        res[algo] = r
        if r:
            print(f"{algo:>5} {inst} T={T:.0f}: feasible={r['feasible']} obj={r['obj']} "
                  f"obj123={r.get('obj123')} wall={r['wall_s']}s", flush=True)
    a, b = order[0], order[1]
    if res.get(a) and res.get(b) and res[a]["obj"] and res[b]["obj"]:
        oa, ob = res[a]["obj"], res[b]["obj"]
        d = 100 * (ob - oa) / oa
        tag = b.upper() if d < -0.05 else (a.upper() if d > 0.05 else "tie")
        print(f"==> {inst} T={T:.0f}: {a} {oa} vs {b} {ob}  ({d:+.1f}% {tag})", flush=True)


if __name__ == "__main__":
    main()
