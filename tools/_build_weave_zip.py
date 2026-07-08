"""Repackage the flat WEAVE submission zip from the current source tree.

    python tools/_build_weave_zip.py [out_name]

Flat layout (7 files): weave/{myalgorithm,weave_engine,weave_ops,portfolio}.py + bridge/{solver,
packing,utils}.py -> zip root. weave_engine/weave_ops append _BRIDGE_DIR (absent in the flat
extract, harmless) so `import solver` resolves to the sibling flat solver.py, exactly as PRISM.
"""
import os, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = {
    "myalgorithm.py": os.path.join(ROOT, "weave", "myalgorithm.py"),
    "weave_engine.py": os.path.join(ROOT, "weave", "weave_engine.py"),
    "weave_ops.py": os.path.join(ROOT, "weave", "weave_ops.py"),
    "portfolio.py": os.path.join(ROOT, "weave", "portfolio.py"),
    # prism_engine is needed by portfolio.py's HYBRID mode (PRISM single-basin workers); in the
    # flat extract it resolves as a sibling import. Harmless (unused) when WEAVE_HYBRID is off.
    "prism_engine.py": os.path.join(ROOT, "prism", "prism_engine.py"),
    "solver.py": os.path.join(ROOT, "bridge", "solver.py"),
    "packing.py": os.path.join(ROOT, "bridge", "packing.py"),
    "utils.py": os.path.join(ROOT, "bridge", "utils.py"),
}
out = sys.argv[1] if len(sys.argv) > 1 else "myalgorithm0702-weave.zip"
outpath = os.path.join(ROOT, out)
with zipfile.ZipFile(outpath, "w", zipfile.ZIP_DEFLATED) as z:
    for arc, src in SRC.items():
        assert os.path.exists(src), f"missing {src}"
        z.write(src, arc)
size = os.path.getsize(outpath)
print(f"built {out} ({size/1024:.1f} KB): {', '.join(SRC)}")
