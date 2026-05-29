---
name: geometry-debug
description: |
  Decompose an OGC2026 stage-2/3/4 feasibility failure into block-, layer-, and
  time-resolved violation records. Use when an eval run shows infeasible
  solutions (feasible=0 with stage in {2,3,4}), when myalgorithm's portfolio
  reports `init.heuristic_result feasible=false stage=2` events, or when the
  user asks "why does block N collide" / "why can't the crane enter at t=K" /
  "what's blocking exit on bay J". Outputs include: per-block obstruction
  records (existing-block id, layer k/j pair, descent-sweep vs final-position,
  overlap area in grid units), boundary violations, and pairwise collision
  records by bay. TRIGGER when the user wants causal explanation of a stage
  failure, not just the stage number. SKIP for stage 1 (assignment validity —
  the violations list is already self-explanatory), for stage 5 (replay —
  same), and for objective tuning (use eval-analyst / improvement-strategist).
---

# Geometry Debug Skill

You are diagnosing **why a solution fails the OGC2026 feasibility check at
stage 2 (crane entry), 3 (crane exit), or 4 (spatial collision)**. The official
`check_feasibility` returns only a stage number and a flat list of human-readable
strings — that's not enough to design a fix. This skill drives
`tools/geometry_debug.py` to unpack the failure.

## When to invoke this skill

- An `instance_results` row has `feasible=0` AND `stage IN ('2','3','4')`.
- An event log has `init.heuristic_result feasible=false stage=2` for any seed,
  especially when *all* seeds in a portfolio fail the same stage.
- The user asks any of:
  - "왜 stage 2가 안 풀려?"
  - "block N이 어디서 막혀?"
  - "EDD가 왜 죽었어?"
  - "decent path 충돌은 어디서?"

## When NOT to invoke

- Stage 1 (assignment validity): the official `violations` strings are already
  precise (`Stage1: block 17 has entry_time < release_time`).
- Stage 5 (replay ordering): same — strings name the offending op.
- Objective regressions where feasibility is preserved: use `eval-analyst`.
- Hypothesis generation: use `improvement-strategist` — this skill produces
  *facts*, not proposals.

## Two input modes

The helper accepts either a solution JSON or constructs one via probe.

### Mode A — drill a known solution

```bash
PYTHONPATH=.codex_deps PYTHONIOENCODING=utf-8 \
  py -3.12 tools/geometry_debug.py \
    --instance alg_tester/example/benchmark/<name>.json \
    --solution path/to/solution.json \
    --limit 25
```

Use this when you have a dumped solution JSON (e.g. from `results_*.json` or
a captured failed-attempt dump).

### Mode B — probe via raw EDD greedy (no repair)

```bash
PYTHONPATH=.codex_deps PYTHONIOENCODING=utf-8 \
  py -3.12 tools/geometry_debug.py \
    --instance alg_tester/example/benchmark/<name>.json \
    --probe-edd --probe-budget 10 \
    --dump-solution tools/debug_dumps/<name>_edd_raw.json \
    --limit 25
```

Use this to reproduce the stage-2 failure that the portfolio's first seed
hits. The `--dump-solution` writes the captured raw greedy output so you can
re-drill later in Mode A without re-running.

## Reading the output

The script emits Markdown-style sections per stage. The shape:

```
# Geometry Debug — instance=<name>
- bays=N, blocks=M, ENTRY ops=..., EXIT ops=...
- check_feasibility -> feasible=False, stage=2, #violations=K

## Stage 1 — Assignment validity
- <strings from check_feasibility, if any>

## Stage 2 — Crane entry feasibility
_Total blocks with stage-2 violations: P (showing up to 25)_

### Block 105 ENTRY @ t=1 bay=0 pos=(40,3) orient=4
  - existing block 35: layers k(new)=[0,1] j(exist)=[0,1] [final=2, descent-sweep=1] max_overlap=48.93
```

Read this as: **block 105's entry at time 1 into bay 0 is blocked by block
35**. The descent path has both *final-position* overlaps (k==j, two layer
pairs) and a *descent-sweep* overlap (j>k, one layer pair). The largest
overlap is ~49 grid units — a structural conflict, not a near-miss.

### Stage-2 / 3 record fields

| Field | Meaning |
|---|---|
| `k(new)` | New-block layer indices that are obstructed |
| `j(exist)` | Existing-block layer indices doing the obstruction |
| `final=X` | Count of (k, j) pairs where k == j (collision at resting position) |
| `descent-sweep=Y` | Count where j > k (existing layer above sweeps new block during descent) |
| `max_overlap` | Largest Shapely intersection area across all (k, j) pairs |
| `BOUNDARY` (label) | New block extends outside the bay polygon |

### Stage-4 record fields

| Field | Meaning |
|---|---|
| `blocks A↔B` | Pairwise overlap between co-present blocks A and B |
| `layer` | Layer index where they overlap |
| `overlap_area` | Shapely intersection area |
| `BOUNDARY violation: block X` | X extends outside its bay (not a pair) |

## Interpretation playbook

After running the helper, classify each violation by **likely root cause** —
this is the value you bring to the strategist. Patterns to look for:

1. **Many large final-position overlaps (`max_overlap > 10`, `final >= 1`)**
   → `_find_earliest_slot` or its monkey-patched variant placed multiple
   blocks at overlapping (x, y) at the same time. The placement scoring is
   broken, not the crane logic.

2. **Predominantly descent-sweep with small overlaps (`max_overlap < 5`,
   `descent-sweep >= 1`, `final == 0`)** → Final-resting positions are clear
   but a taller existing block's upper layer (`j > 0`) blocks the new block's
   descent. The crane geometry needs more vertical clearance or different
   timing.

3. **One existing block obstructs many new blocks** → That existing block
   was placed too early / too centrally; it occupies a critical strip during
   peak load. Consider deferring or re-locating it.

4. **All violations cluster at one time point (e.g. `t=2` repeatedly)** → The
   schedule packs too many concurrent entries into the same window; the
   issue is temporal, not spatial.

5. **`BOUNDARY` on a block whose footprint is near `bay_width-1`** →
   The orient choice or x offset made the block stick out. Constrain orient
   selection in the placement scorer.

## Output format expected of you

After driving the helper, reply to the user with:

1. **Headline** — one sentence: stage, count, dominant pattern. Example:
   "Stage 2 on bench_B5_b120: 9 blocks fail, 7 are large final-position
   overlaps (greedy placement collision)."
2. **Top-3 violations table** — block id, blocker id, area, k/j, sweep/final.
3. **Pattern classification** — which of the 5 playbook items above fits.
4. **Pointer for next agent** — one sentence aimed at
   `improvement-strategist`: "Target locus: monkey-patched
   `custom_find_earliest_slot` x/y candidate enumeration."

Do NOT propose code changes — that's `improvement-strategist`'s job. Do NOT
re-run the eval. Do NOT modify utils.py.

## Hard rules

- Always cite the helper's output. Quote block ids and overlap areas
  verbatim — no rounding to "small" or "large" without numbers.
- If the helper crashes or returns no violations on a known-infeasible
  solution, report that as an anomaly; don't paper over it.
- Length cap: under 350 words for the user-facing summary.
- One skill invocation per instance. If the user wants multiple instances
  drilled, batch the helper calls in one Bash pipe.
