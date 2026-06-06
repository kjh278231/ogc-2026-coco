# scratch/

Cross-session memory for the improvement-loop agents.

| File | Written by | Purpose |
|---|---|---|
| `hypotheses_history.jsonl` | improvement-strategist | every hypothesis ever proposed; used as anti-pattern memory |
| `implemented.jsonl` | solver-developer | every hypothesis that landed as code |
| `verdicts.jsonl` | approval-gate | every gate decision |
| `rejected.jsonl` | user, on REJECT | hypotheses that the gate rejected, with reason |

All files are append-only JSONL. One JSON object per line. Schemas are documented in each agent's `.md`.

This directory is committed (not gitignored) so the project history of attempts survives across machines and contributors.
