#!/bin/bash
# Overrun-safety check: PRISM+MO at the LONGEST grader budget (T=300, the P4-P6 regime) on the
# heaviest-packing instances. The multi-order final build packs tardy bays best-of-4-orders, so
# the build reserve must still finish before poly_dl. PASS = wall <= T and feasible (stage 5).
cd /c/Users/ADMIN/Workspace/ogc2026
T=${1:-300}; OUT=$2
echo "# PRISM+MO overrun check T=$T  $(date)" > "$OUT"
for inst in T20 T14 T38 T18; do
  line=$(SOLVER_MULTIORDER=1 ./.venv/Scripts/python.exe tools/_prism_portf_ab.py prism $inst $T 2>/dev/null)
  echo "$line" >> "$OUT"
  echo "$inst done (wall in line; must be <= $T)" >&2
done
echo "ALL DONE" >> "$OUT"
