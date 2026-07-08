#!/bin/sh
# HELM wall A/B session (strictly sequential; close-in-time pairs; zombie-free between runs).
#   sh tools/_helm_wall_run.sh > tools/_helm_wall_ab.txt 2>&1
cd "$(dirname "$0")/.." || exit 1
PY=./.venv/Scripts/python.exe

echo "=== gate: band losses at deployed wall (T=180) ==="
"$PY" tools/_helm_wall_ab.py T18 180 prism,helm
"$PY" tools/_helm_wall_ab.py T6  180 prism,helm

echo "=== sanity: known FLUX wall win + tie ==="
"$PY" tools/_helm_wall_ab.py T13 180 prism,helm
"$PY" tools/_helm_wall_ab.py T20 180 prism,helm

echo "=== P4/P5 zone (T=300) ==="
"$PY" tools/_helm_wall_ab.py T14 300 prism,helm

echo "=== irreducible T38 (T=300): champion vs HELM vs HELM+MO-off ==="
"$PY" tools/_prism_portf_ab.py prism T38 300
"$PY" tools/_prism_portf_ab.py helm  T38 300
SOLVER_MULTIORDER=0 "$PY" tools/_prism_portf_ab.py helm T38 300
echo "=== DONE ==="
