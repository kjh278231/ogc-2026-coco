import os, sys, json, time, zipfile
if __name__ == "__main__":
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # zip path overridable as argv[3] (default = the 0629 PRISM zip, unchanged)
    zpath = sys.argv[3] if len(sys.argv) > 3 else os.path.join(ROOT, "myalgorithm0629-prism.zip")
    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "prism_zip_extract")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(zpath) as z: z.extractall(tmp)
    sys.path.insert(0, tmp)
    inst = sys.argv[1] if len(sys.argv) > 1 else "T20"
    T = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
    prob = json.load(open(os.path.join(ROOT, "train", f"{inst}.json"), encoding="utf-8"))
    import myalgorithm
    t0 = time.time(); sol = myalgorithm.algorithm(prob, T); wall = time.time() - t0
    import utils
    r = utils.check_feasibility(prob, sol)
    print(json.dumps({"inst": inst, "T": T, "feasible": r["feasible"], "stage": r["stage"],
                      "obj": round(r["objective"]) if r.get("objective") else None,
                      "wall_s": round(wall, 1), "overrun": wall > T,
                      "extracted_from": os.path.dirname(myalgorithm.__file__)}))
