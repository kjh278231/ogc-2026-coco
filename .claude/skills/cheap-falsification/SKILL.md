---
name: cheap-falsification
description: Use BEFORE building any solver/heuristic, when you have a hypothesis about where the difficulty of the OGC problem lives (e.g. "tardiness is driven by X", "metric Y predicts Z"). Picks the cheapest experiment that could falsify the hypothesis and runs it. Do not skip to implementation while a load-bearing premise is unverified.
---

# Cheap falsification

Kill a hypothesis with the cheapest possible measurement *before* investing in a solver.
Falsification is cheap; confirmation is expensive. A solver built on a false premise wastes
the most effort.

## Procedure

1. **State the hypothesis as falsifiable.** Not "it's hard" but "tardiness comes from temporal
   slack" / "per-bay area load predicts tardiness" / "the bottleneck is crane EXIT". Write down
   what result would *disprove* it.

2. **Pick the cheapest tool that can falsify it**, in this order:
   - **Data only** (no solver): compute bounds/correlations directly from `train/*.json`
     (e.g. `Σmax(0,R+P−D)`, area-vs-capacity, slack distribution). Cheapest — prefer this.
   - **Reuse an existing tool as an instrument**: run the organizer baseline
     (`baseline/baseline_greedy.greedyalgorithm`) and measure, don't build new.
   - **Minimal new probe**: a throwaway measurement script (not a solver) — last resort.

3. **Run it** with the project venv: `./.venv/Scripts/python.exe .claude/scratch/<probe>.py`.
   Geometry/feasibility needs shapely (present in `.venv`, not system python).

4. **Read the result as a narrowing signal.** A dead hypothesis points at the next axis. Record
   the verdict in memory (`memory/`) so the chain compounds across the session.

## Notes specific to OGC

- Bays are feasibility-independent and the objective separates
  (`Z2`,`Z3` = f(assignment); `Z1` = Σ per-bay) — exploit this to make experiments local.
- Many cheap aggregate bounds are *vacuous* here (temporal LB=0, area LB=0, per-bay area
  ρ=0.19). If a bound is vacuous, that itself is the finding: the signal lives in actual
  geometry/packing, not aggregates.
- Worked examples: `docs/methodology.md` §1 (Exp 0 / 2 / A / B).
