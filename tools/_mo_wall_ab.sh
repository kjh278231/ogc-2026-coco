#!/bin/bash
# Wall A/B: multiorder off vs on, real time-managed single-process path.
cd /c/Users/ADMIN/Workspace/ogc2026
T=$1
OUT=$2
echo "# wall A/B T=$T  (off vs on)  $(date)" > "$OUT"
for inst in T2 T1 T6 T11 T9 T18 T13 T14 T20; do
  off=$(./.venv/Scripts/python.exe tools/_mo_run.py $inst wall:$T 0 2>/dev/null | grep RESULT)
  on=$(./.venv/Scripts/python.exe tools/_mo_run.py $inst wall:$T 1 2>/dev/null | grep RESULT)
  echo "$inst OFF $off" >> "$OUT"
  echo "$inst ON  $on" >> "$OUT"
  echo "$inst done" >&2
done
echo "ALL DONE" >> "$OUT"
