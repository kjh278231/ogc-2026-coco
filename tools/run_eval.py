"""Generic solver evaluation runner for the OGC 2026 repo.

Runs <solver_dir>/myalgorithm.algorithm over a set of instances, validates EVERY
solution with the OFFICIAL checker (alg_tester/utils.py check_feasibility -- never the
solver's self-computed objective), and writes a results JSON. Optionally compares
per-instance against a reference results JSON (non-regression guard).

Usage:
  python tools/run_eval.py --solver prism --instances "train/*.json" --timelimit 60 \
      --out results_prism_t60.json [--compare results_bridge_t60.json]

  # quick smoke (1 instance, short budget) -- always do this before a long run:
  python tools/run_eval.py --solver prism --instances train/T1.json --timelimit 10 --out -

Notes:
  - Each instance runs in a FRESH subprocess (module/env isolation between solver runs,
    and a hard kill at ~3x timelimit + 120s so one hang cannot eat the whole batch).
  - The official checker is loaded by file path under a private module name, so it never
    collides with a solver's own utils.py.
  - Comparisons are only meaningful at the SAME timelimit; the tool refuses --compare
    when the reference was produced with a different timelimit.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = "RUN_EVAL_RESULT "


def _single(solver_dir, prob_path, timelimit):
    """Child mode: run one instance, print a marker JSON line, exit."""
    sys.path.insert(0, os.path.join(ROOT, solver_dir))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_official_checker", os.path.join(ROOT, "alg_tester", "utils.py"))
    checker = importlib.util.module_from_spec(spec)
    # must be registered BEFORE exec: the checker uses dataclasses, whose field-type
    # resolution looks the module up in sys.modules (else: AttributeError on NoneType).
    sys.modules["_official_checker"] = checker
    spec.loader.exec_module(checker)

    import myalgorithm
    prob = json.load(open(prob_path, encoding="utf-8"))
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, timelimit=timelimit)
    wall = time.time() - t0
    chk = checker.check_feasibility(prob, sol)
    rec = {
        "feasible": bool(chk.get("feasible")),
        "objective": chk.get("objective"),
        "obj1": chk.get("obj1"), "obj2": chk.get("obj2"), "obj3": chk.get("obj3"),
        "wall": round(wall, 1),
    }
    print(MARKER + json.dumps(rec), flush=True)


def _run_instance(solver, path, timelimit):
    kill_after = timelimit * 3 + 120
    cmd = [sys.executable, os.path.abspath(__file__),
           "--single", solver, path, str(timelimit)]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=kill_after, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {"error": "killed after %ds (hang?)" % kill_after}
    for ln in reversed((cp.stdout or "").splitlines()):
        if ln.startswith(MARKER):
            return json.loads(ln[len(MARKER):])
    tail = "\n".join(((cp.stderr or "") + (cp.stdout or "")).splitlines()[-8:])
    return {"error": "no result (exit=%s)\n%s" % (cp.returncode, tail)}


def _inst_key(name):
    m = re.search(r"(\d+)", name)
    return (0, int(m.group(1))) if m else (1, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--single", nargs=3, metavar=("SOLVER", "PROB", "TL"),
                    help="internal: run one instance and print a marker line")
    ap.add_argument("--solver", help="solver directory name (bridge/prism/helm/...)")
    ap.add_argument("--instances", default="train/*.json")
    ap.add_argument("--timelimit", type=float, default=60)
    ap.add_argument("--out", default="-", help="output JSON path, or - for stdout only")
    ap.add_argument("--compare", help="reference results JSON (same timelimit required)")
    args = ap.parse_args()

    if args.single:
        _single(args.single[0], args.single[1], float(args.single[2]))
        return

    if not args.solver:
        ap.error("--solver is required")
    if not os.path.isfile(os.path.join(ROOT, args.solver, "myalgorithm.py")):
        ap.error("no myalgorithm.py in %r" % args.solver)

    paths = sorted(glob.glob(os.path.join(ROOT, args.instances)),
                   key=lambda p: _inst_key(os.path.basename(p)))
    if not paths:
        ap.error("no instances match %r" % args.instances)

    ref = None
    if args.compare:
        ref = json.load(open(args.compare, encoding="utf-8"))
        if ref.get("meta", {}).get("timelimit") != args.timelimit:
            sys.exit("refusing to compare: reference timelimit=%s != %s "
                     "(different budgets are not comparable)"
                     % (ref.get("meta", {}).get("timelimit"), args.timelimit))

    results = {}
    print(f"{'inst':>6} {'feas':>5} {'objective':>12} {'obj1':>9} {'obj2':>9} "
          f"{'obj3':>9} {'wall':>7}" + ("  vs ref" if ref else ""), flush=True)
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        rec = _run_instance(args.solver, path, args.timelimit)
        results[name] = rec
        if "error" in rec:
            print(f"{name:>6} ERROR {rec['error']}", flush=True)
            continue
        line = (f"{name:>6} {str(rec['feasible']):>5} {rec['objective']:>12.0f} "
                f"{rec['obj1']:>9.0f} {rec['obj2']:>9.1f} {rec['obj3']:>9.0f} "
                f"{rec['wall']:>6.1f}s")
        if ref and name in ref.get("results", {}) and ref["results"][name].get("objective"):
            r = ref["results"][name]["objective"]
            d = (rec["objective"] - r) / r * 100 if r else 0.0
            line += f"  {d:+6.1f}%"
        print(line, flush=True)

    ok = [n for n, r in results.items() if r.get("feasible")]
    bad = [n for n, r in results.items() if not r.get("feasible")]
    over = [n for n, r in results.items()
            if r.get("wall") and r["wall"] > args.timelimit * 1.05]
    print("-" * 60)
    print(f"feasible {len(ok)}/{len(results)}"
          + (f" | INFEASIBLE/ERROR: {', '.join(bad)}" if bad else "")
          + (f" | wall>limit: {', '.join(over)}" if over else ""), flush=True)

    if ref:
        wins = losses = ties = 0
        tot_new = tot_ref = 0.0
        for n, r in results.items():
            rr = ref.get("results", {}).get(n, {})
            if not (r.get("objective") is not None and rr.get("objective")):
                continue
            tot_new += r["objective"]; tot_ref += rr["objective"]
            if r["objective"] < rr["objective"] - 0.5:
                wins += 1
            elif r["objective"] > rr["objective"] + 0.5:
                losses += 1
            else:
                ties += 1
        if tot_ref:
            print(f"vs {os.path.basename(args.compare)}: {wins}W/{losses}L/{ties}T | "
                  f"aggregate {(tot_new - tot_ref) / tot_ref * 100:+.1f}%", flush=True)

    if args.out != "-":
        payload = {"meta": {"solver": args.solver, "timelimit": args.timelimit,
                            "instances": args.instances,
                            "date": time.strftime("%Y-%m-%d %H:%M")},
                   "results": results}
        outp = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        json.dump(payload, open(outp, "w", encoding="utf-8"), indent=1)
        print("wrote", outp, flush=True)


if __name__ == "__main__":
    main()
