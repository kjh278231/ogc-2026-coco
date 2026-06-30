#!/bin/bash
# Extra confidence before grader submission: PRISM vs PRISM+MO on the UNTESTED easy/small
# instances at T=60. These have low Z1, so multi-order has little to gain and could (in
# principle) only spend throughput -> the likeliest place for a regression. Confirm none.
cd /c/Users/ADMIN/Workspace/ogc2026
T=${1:-60}; OUT=$2
echo "# PRISM off vs PRISM+MO  T=$T  (easy/untested set)  $(date)" > "$OUT"
for inst in T3 T4 T5 T7 T8 T10 T12 T16; do
  off=$(SOLVER_MULTIORDER=0 ./.venv/Scripts/python.exe tools/_prism_portf_ab.py prism $inst $T 2>/dev/null)
  on=$( SOLVER_MULTIORDER=1 ./.venv/Scripts/python.exe tools/_prism_portf_ab.py prism $inst $T 2>/dev/null)
  echo "OFF $off" >> "$OUT"
  echo "ON  $on" >> "$OUT"
  echo "$inst done" >&2
done
echo "ALL DONE" >> "$OUT"
