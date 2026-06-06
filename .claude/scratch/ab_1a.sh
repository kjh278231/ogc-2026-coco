#!/usr/bin/env bash
# Controlled A/B for 1-A only (z2 area-weighting, 1-C reverted) vs committed
# baseline. Same parallel conditions. Order: 1-A (working tree) -> stash solver
# -> baseline -> pop. trap guarantees the stash is popped (working tree restored
# to the 1-A edits) even on failure.
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

echo "=== [1/2] 1-A eval (working tree = z2 weighting only) ==="
py -3.12 tools/parallel_eval.py --algo athena --timelimit 60 \
    --pattern "*.json" --note "1-A only: z2 area-weighting (1-C reverted)"
echo "1A exit=$?"

echo "=== stashing $SOLVER to recover committed baseline ==="
git stash push -m "ab-1a-temp" -- "$SOLVER"
STASHED=1
git status --short "$SOLVER"

echo "=== [2/2] baseline eval (committed solver) ==="
py -3.12 tools/parallel_eval.py --algo athena --timelimit 60 \
    --pattern "*.json" --note "baseline pre-step0 (committed solver) [1-A A/B]"
echo "baseline exit=$?"
echo "=== A/B DONE ==="
