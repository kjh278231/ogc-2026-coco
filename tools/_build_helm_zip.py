"""Repackage the flat HELM submission zip from the current source tree.

    python tools/_build_helm_zip.py [out_name]

Flat layout (7 files): helm/{myalgorithm,helm_engine,portfolio}.py + flux/flux_engine.py +
bridge/{solver,packing,utils}.py -> zip root. helm_engine appends _FLUX_DIR only when it
exists (absent in the flat extract), so `import flux_engine` resolves to the sibling flat
flux_engine.py, which in turn resolves `import solver` the same way -- same pattern as the
PRISM/WEAVE/FLUX zips.
"""
import os, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = {
    "myalgorithm.py": os.path.join(ROOT, "helm", "myalgorithm.py"),
    "helm_engine.py": os.path.join(ROOT, "helm", "helm_engine.py"),
    "portfolio.py": os.path.join(ROOT, "helm", "portfolio.py"),
    "flux_engine.py": os.path.join(ROOT, "flux", "flux_engine.py"),
    "solver.py": os.path.join(ROOT, "bridge", "solver.py"),
    "packing.py": os.path.join(ROOT, "bridge", "packing.py"),
    "utils.py": os.path.join(ROOT, "bridge", "utils.py"),
}
out = sys.argv[1] if len(sys.argv) > 1 else "myalgorithm0703-helm.zip"
outpath = os.path.join(ROOT, out)
with zipfile.ZipFile(outpath, "w", zipfile.ZIP_DEFLATED) as z:
    for arc, src in SRC.items():
        assert os.path.exists(src), f"missing {src}"
        z.write(src, arc)
size = os.path.getsize(outpath)
print(f"built {out} ({size/1024:.1f} KB): {', '.join(SRC)}")
