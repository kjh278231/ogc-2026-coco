# OGC2026 Improvement Loop — Agent Workflow

Four project sub-agents that together form a measurable improvement loop on top of `tools/eval_runner.py` and `tools/eval_summary.py`. Each agent is intentionally narrow; the orchestration is done by the user (or the parent Claude session) handing structured artifacts between them.

## The loop

```
        ┌──────────────────────────────┐
        │  tools/eval_runner.py        │  populates SQLite + JSONL
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  eval-analyst (haiku)        │  data → structured Markdown report
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  improvement-strategist      │  report + codebase → 1–3 hypotheses (JSON)
        │  (opus)                      │
        └──────────────┬───────────────┘
                       ↓
            user picks one
                       ↓
        ┌──────────────────────────────┐
        │  solver-developer (sonnet)   │  hypothesis → minimal code change
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  tools/eval_runner.py (again)│  populates new run
        └──────────────┬───────────────┘
                       ↓
        ┌──────────────────────────────┐
        │  approval-gate (sonnet)      │  before/after runs → APPROVE / REJECT / REVIEW
        └──────────────────────────────┘
                       │
              APPROVE  ↓        REJECT → revert (git revert) → loop back to strategist
                      next hypothesis
```

## Agent roster

| Agent | Model | Tools | What it reads | What it produces |
|---|---|---|---|---|
| `eval-analyst` | haiku | Bash, Read, Grep | SQLite + JSONL | Markdown eval report |
| `improvement-strategist` | opus | Read, Grep, Glob, Bash, WebSearch, WebFetch | Eval report + code + history | JSON array of hypotheses |
| `solver-developer` | sonnet | Read, Edit, Write, Grep, Glob, Bash | One hypothesis JSON | Code change + Markdown summary |
| `approval-gate` | sonnet | Bash, Read | Two run_ids in SQLite | APPROVE/REJECT/REVIEW verdict |

## Invocation patterns (the routing model picks these up from each agent's `description:`)

| You say... | Routed to |
|---|---|
| "summarize run 5" / "how did the last eval go?" | `eval-analyst` |
| "what next?" / "propose hypotheses" / "suggest changes" | `improvement-strategist` |
| "implement H-007" / "apply this hypothesis" | `solver-developer` |
| "gate check run 8 vs 7" / "approve H-007" / "should we merge?" | `approval-gate` |

## Scratch directory contracts

The agents persist artifacts under `.claude/scratch/` so the loop has cross-session memory:

| File | Owner (write) | Reader (read) | Format |
|---|---|---|---|
| `hypotheses_history.jsonl` | improvement-strategist | improvement-strategist (anti-pattern), approval-gate (lookup) | one hypothesis JSON per line |
| `implemented.jsonl` | solver-developer | approval-gate | `{hypothesis_id, files, git_sha_before, implemented_at}` per line |
| `verdicts.jsonl` | approval-gate | improvement-strategist (track rejection causes) | `{target_run, baseline_run, decision, ...}` per line |
| `rejected.jsonl` | (user, on REJECT) | improvement-strategist | `{hypothesis_id, why}` per line |

All scratch files are append-only. None are gitignored by default — the user decides whether to commit them.

## Manual happy-path (today)

The loop is not auto-driven yet. Today the human triggers each step:

```bash
# 0. (one-time) Make sure the env is active.
conda activate ogc2026

# 1. Run an eval.
python tools/eval_runner.py --timelimit 30 --pattern "bench_*.json" --note "<context>"

# 2. Ask the eval-analyst.
#    (In Claude Code: "summarize the last run")

# 3. Ask the improvement-strategist.
#    (In Claude Code: "given that report, propose hypotheses")

# 4. Pick one. Then:
#    (In Claude Code: "implement H-NNN")

# 5. Re-run the eval on the same pattern.
python tools/eval_runner.py --timelimit 30 --pattern "bench_*.json" --note "post H-NNN"

# 6. Gate check.
#    (In Claude Code: "approve H-NNN" or "gate check run <new> vs <prev>")
```

## Design constraints (don't violate these when editing the agents)

- **No agent modifies the eval data.** Only `tools/eval_runner.py` writes to the SQLite DB.
- **Each agent is single-purpose.** If you find yourself adding a second responsibility to an agent, split it.
- **Hypotheses are immutable once recorded.** If a hypothesis evolves, give it a new ID.
- **Rules over judgment in the gate.** New rules go into `approval-gate.md`'s rule list; do not add inline LLM judgment around existing rules.
- **`utils.py` is sacred** — no agent ever proposes or applies edits to it. State this in the agent body if adding a new agent.
