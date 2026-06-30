#!/bin/bash
# Validation gate before grader submission: PRISM (MO off) vs PRISM+MO at a SHORT budget
# (T=60 = the P1/P2 regime where multi-order's per-eval throughput cost is riskiest). The
# T=180 win is established; this confirms no short-budget / easy-instance regression.
# Interleaved off/on per instance (wall-noise controlled). Explicit SOLVER_MULTIORDER overrides
# the new default in prism/myalgorithm.py.
cd /c/Users/ADMIN/Workspace/ogc2026
T=$1; OUT=$2
echo "# PRISM off vs PRISM+MO  T=$T  $(date)" > "$OUT"
for inst in T1 T2 T6 T13 T17 T20; do
  off=$(SOLVER_MULTIORDER=0 ./.venv/Scripts/python.exe tools/_prism_portf_ab.py prism $inst $T 2>/dev/null)
  on=$( SOLVER_MULTIORDER=1 ./.venv/Scripts/python.exe tools/_prism_portf_ab.py prism $inst $T 2>/dev/null)
  echo "OFF $off" >> "$OUT"
  echo "ON  $on" >> "$OUT"
  echo "$inst done" >&2
done
echo "ALL DONE" >> "$OUT"
