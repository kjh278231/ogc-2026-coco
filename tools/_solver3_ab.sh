#!/bin/bash
# 3-way portfolio A/B at fixed T: bridge (seed-diverse) vs prism (anchor-diverse) vs
# stow (packing-diverse). Interleaved per instance so wall noise hits all three equally.
cd /c/Users/ADMIN/Workspace/ogc2026
T=$1
OUT=$2
echo "# 3-way portfolio A/B T=$T  bridge|prism|stow  $(date)" > "$OUT"
for inst in T1 T9 T11 T13 T18 T20; do
  for algo in bridge prism stow; do
    line=$(./.venv/Scripts/python.exe tools/_prism_portf_ab.py $algo $inst $T 2>/dev/null)
    echo "$line" >> "$OUT"
    echo "$inst/$algo done" >&2
  done
done
echo "ALL DONE" >> "$OUT"
