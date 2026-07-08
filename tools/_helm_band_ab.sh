#!/bin/sh
# PRISM vs FLUX eval-count A/B on the CONGESTED-band instances whose head-to-head was never
# measured (router gate #2). Strictly sequential (single-use Gurobi license). E=2500.
#   sh tools/_helm_band_ab.sh > tools/_helm_band_ab.txt 2>&1
cd "$(dirname "$0")/.." || exit 1
PY=./.venv/Scripts/python.exe
for inst in T6 T7 T10 T12 T15 T16 T17 T18 T19; do
  for eng in prism flux; do
    "$PY" tools/_helm_eval_ab.py "$eng" "$inst" 2500 2>/dev/null
  done
done
