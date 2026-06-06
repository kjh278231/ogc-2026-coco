#!/usr/bin/env bash
# Controlled A/B for Step 0 (1-A + 1-C) on Athena, same parallel conditions.
# Order: Step0 (current working tree) -> stash solver -> baseline -> pop.
# A trap guarantees the stash is popped even on failure, so the working tree
# is always restored to the Step0 edits.
set -u
cd /c/Users/ADMIN/Workspace/ogc2026

export PYTHONPATH=.codex_deps
export PYTHONIOENCODING=utf-8

SOLVER=baseline/my_new_algorithm.py
STASHED=0

restore() {
  if [ "$STASHED" = "1" ]; then
    echo "=== [restore] git stash pop ==="
    git stash pop
    STASHED=0
  fi
}
trap restore EXIT

echo "=== [1/2] Step0 eval (working tree = 1-A+1-C) ==="
py -3.12 tools/parallel_eval.py --algo athena --timelimit 60 \
    --pattern "*.json" --note "step0 1-A z2-weight + 1-C distinct-bay cap"
echo "step0 exit=$?"

echo "=== stashing $SOLVER to recover pre-Step0 baseline ==="
git stash push -m "step0-ab-temp" -- "$SOLVER"
STASHED=1
echo "--- solver now at committed baseline; git status: ---"
git status --short "$SOLVER"

echo "=== [2/2] baseline eval (committed solver) ==="
py -3.12 tools/parallel_eval.py --algo athena --timelimit 60 \
    --pattern "*.json" --note "baseline pre-step0 (committed solver)"
echo "baseline exit=$?"

# restore() runs on EXIT
echo "=== A/B DONE ==="
