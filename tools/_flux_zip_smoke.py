"""Build the flat FLUX zip, extract it, and smoke-test the extracted flat layout end-to-end
(the real grader entry: import myalgorithm; algorithm(prob, T)) in a subprocess whose cwd is the
extract dir -- so it exercises the flat import resolution (flux_engine's append of a nonexistent
_BRIDGE_DIR + `import solver` finding the sibling flat solver.py) and the portfolio spawn.

    python tools/_flux_zip_smoke.py [inst:T ...]   (default: T1:60 T13:180 T20:300)
"""
import os, sys, json, zipfile, tempfile, subprocess, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

# 1. build the zip
zip_out = os.path.join(ROOT, "myalgorithm0702-flux.zip")
subprocess.run([PY, os.path.join(ROOT, "tools", "_build_flux_zip.py"),
                os.path.basename(zip_out)], cwd=ROOT, check=True)

# 2. extract to a temp dir
tmp = tempfile.mkdtemp(prefix="flux_zip_smoke_")
with zipfile.ZipFile(zip_out) as z:
    z.extractall(tmp)
print("extracted:", sorted(os.listdir(tmp)), flush=True)

# 3. write a runner INSIDE the extract dir (flat) and run it there
runner = os.path.join(tmp, "_run.py")
with open(runner, "w") as f:
    # NOTE: __main__-guarded -- the portfolio uses multiprocessing 'spawn', which RE-IMPORTS this
    # file as __main__ in each worker. Without the guard the workers would re-run algorithm() =>
    # recursive spawn => the spawn-probe fails => serial fallback (which over-reserves the poly
    # build and finishes early with a worse obj). The guard makes the smoke exercise the real
    # portfolio path, exactly as a __main__-guarded grader harness would. See portfolio-spawn-guard.
    f.write(
        "import sys, json, time\n"
        "def main():\n"
        "    inst, T = sys.argv[1], float(sys.argv[2])\n"
        "    prob = json.load(open(sys.argv[3], encoding='utf-8'))\n"
        "    import myalgorithm as M\n"
        "    t0 = time.time(); sol = M.algorithm(prob, T); wall = time.time()-t0\n"
        "    import utils\n"
        "    r = utils.check_feasibility(prob, sol)\n"
        "    print(json.dumps({'inst': inst, 'T': T, 'feasible': r['feasible'],\n"
        "          'obj': round(r['objective']) if r.get('objective') else None,\n"
        "          'wall_s': round(wall,1), 'margin_s': round(T-wall,1)}))\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )

jobs = sys.argv[1:] or ["T1:60", "T13:180", "T20:300"]
ok = True
for job in jobs:
    inst, T = job.split(":")
    ipath = os.path.join(ROOT, "train", f"{inst}.json")
    p = subprocess.run([PY, "_run.py", inst, T, ipath], cwd=tmp,
                       capture_output=True, text=True, timeout=float(T) + 120)
    line = next((l for l in p.stdout.splitlines() if l.strip().startswith("{")), None)
    if line:
        r = json.loads(line)
        overrun = r["wall_s"] > float(T) + 1.0
        flag = "OK" if (r["feasible"] and not overrun) else "!!"
        if not r["feasible"] or overrun:
            ok = False
        print(f"  [{flag}] {inst}@{T}: feasible={r['feasible']} obj={r['obj']} "
              f"wall={r['wall_s']}s margin={r['margin_s']}s", flush=True)
    else:
        ok = False
        print(f"  [!!] {inst}@{T}: NO OUTPUT. stderr:\n{p.stderr[-800:]}", flush=True)

shutil.rmtree(tmp, ignore_errors=True)
print("SMOKE", "PASS" if ok else "FAIL", flush=True)
