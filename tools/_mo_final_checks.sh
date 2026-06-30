#!/bin/bash
# Pre-submission gate for the PRISM+MO zip: extract the ACTUAL submission artifact and run it
# at the LONGEST grader budget (T=300, P4-P6) on the heaviest instances. The multi-order final
# build packs tardy bays best-of-4-orders, so this confirms the build reserve still finishes in
# time. PASS = feasible (stage 5) AND overrun=false on every line.
cd /c/Users/ADMIN/Workspace/ogc2026
ZIP=${1:-myalgorithm0701-prism-multiorder.zip}
OUT=$2
echo "# PRISM+MO zip pre-submission smoke ($ZIP)  $(date)" > "$OUT"
for inst in T20 T14 T18; do
  line=$(./.venv/Scripts/python.exe tools/_prism_zip_smoke.py $inst 300 "$ZIP" 2>/dev/null)
  echo "$line" >> "$OUT"
  echo "$inst@300 done" >&2
done
# also a short budget from the extracted artifact (P1 regime)
line=$(./.venv/Scripts/python.exe tools/_prism_zip_smoke.py T2 60 "$ZIP" 2>/dev/null)
echo "$line" >> "$OUT"
echo "ALL DONE" >> "$OUT"
