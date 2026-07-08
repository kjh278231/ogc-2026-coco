"""Repackage the flat FLUX submission zip from the current source tree.

    python tools/_build_flux_zip.py [out_name]

Flat layout (6 files): flux/{myalgorithm,flux_engine,portfolio}.py + bridge/{solver,packing,
utils}.py -> zip root. flux_engine appends _BRIDGE_DIR (absent in the flat extract) so `import
solver` resolves to the sibling flat solver.py -- same pattern as the PRISM/WEAVE zips.
"""
import os, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = {
    "myalgorithm.py": os.path.join(ROOT, "flux", "myalgorithm.py"),
    "flux_engine.py": os.path.join(ROOT, "flux", "flux_engine.py"),
    "portfolio.py": os.path.join(ROOT, "flux", "portfolio.py"),
    "solver.py": os.path.join(ROOT, "bridge", "solver.py"),
    "packing.py": os.path.join(ROOT, "bridge", "packing.py"),
    "utils.py": os.path.join(ROOT, "bridge", "utils.py"),
}
out = sys.argv[1] if len(sys.argv) > 1 else "myalgorithm0702-flux.zip"
outpath = os.path.join(ROOT, out)
with zipfile.ZipFile(outpath, "w", zipfile.ZIP_DEFLATED) as z:
    for arc, src in SRC.items():
        assert os.path.exists(src), f"missing {src}"
        z.write(src, arc)
size = os.path.getsize(outpath)
print(f"built {out} ({size/1024:.1f} KB): {', '.join(SRC)}")
