#!/bin/bash
# Next lever: does a CHEAPER 2-order multi-order ({EDD,release}) beat the 4-order default in the
# PRISM+MO portfolio? 2 packs/tardy-bay instead of 4 -> workers do more evals/wall; `release`
# alone captured -19% of the -24% order-oracle, so quality should hold. Both runs are PRISM+MO
# (SOLVER_MULTIORDER=1); only SOLVER_MO_ORDERS differs. True grader obj, T=180.
cd /c/Users/ADMIN/Workspace/ogc2026
T=${1:-180}; OUT=$2
echo "# PRISM+MO 4-order vs 2-order(edd,release)  T=$T  $(date)" > "$OUT"
for inst in T1 T11 T13 T18 T20; do
  four=$(SOLVER_MULTIORDER=1 ./.venv/Scripts/python.exe tools/_prism_portf_ab.py prism $inst $T 2>/dev/null)
  two=$( SOLVER_MULTIORDER=1 SOLVER_MO_ORDERS="edd,release" ./.venv/Scripts/python.exe tools/_prism_portf_ab.py prism $inst $T 2>/dev/null)
  echo "4ord $four" >> "$OUT"
  echo "2ord $two" >> "$OUT"
  echo "$inst done" >&2
done
echo "ALL DONE" >> "$OUT"
