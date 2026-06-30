#!/bin/bash
# Deterministic eval-count multi-order A/B over an arbitrary instance list.
# Usage: _mo_eval_list.sh <E> <OUT> <inst1> <inst2> ...
cd /c/Users/ADMIN/Workspace/ogc2026
E=$1; OUT=$2; shift 2
echo "# eval-count A/B E=$E (off vs on) deterministic  $(date)" > "$OUT"
for inst in "$@"; do
  off=$(./.venv/Scripts/python.exe tools/_mo_run.py $inst $E 0 2>/dev/null | grep RESULT)
  on=$(./.venv/Scripts/python.exe tools/_mo_run.py $inst $E 1 2>/dev/null | grep RESULT)
  echo "$inst OFF $off" >> "$OUT"
  echo "$inst ON  $on" >> "$OUT"
  echo "$inst done" >&2
done
echo "ALL DONE" >> "$OUT"
