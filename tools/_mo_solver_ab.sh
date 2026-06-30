#!/bin/bash
# Existing portfolios WITH multi-order packing on (SOLVER_MULTIORDER=1 -> all workers+master
# inherit it via the shared kernel). Tests goal #2: does enabling multi-order improve PRISM
# (the champion, which keeps its MIP anchors -> T11) and BRIDGE? Same 6 instances as the
# 3-way A/B so the numbers line up against bridge/prism/stow in _solver3_T180.txt.
cd /c/Users/ADMIN/Workspace/ogc2026
T=$1; OUT=$2
echo "# PRISM+MO / BRIDGE+MO  T=$T  (SOLVER_MULTIORDER=1)  $(date)" > "$OUT"
for inst in T1 T9 T11 T13 T18 T20; do
  for algo in prism bridge; do
    line=$(SOLVER_MULTIORDER=1 ./.venv/Scripts/python.exe tools/_prism_portf_ab.py $algo $inst $T 2>/dev/null)
    echo "${algo}MO $line" >> "$OUT"
    echo "$inst/${algo}MO done" >&2
  done
done
echo "ALL DONE" >> "$OUT"
