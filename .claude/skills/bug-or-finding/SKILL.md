---
name: bug-or-finding
description: Use when an experiment produces an extreme, surprising, or too-good/too-bad result before reporting or acting on it. Decide whether it is a genuine finding or a bug by checking it against a theoretical impossibility argument. Prevents reporting artifacts as conclusions.
---

# Bug-or-finding

An extreme result is more often a bug than a discovery. Before reporting or building on it,
try to *prove it impossible*. If a short argument says the result cannot happen for correct
code, it is a bug — find it; do not report it.

## Procedure

1. **Sanity-bound the result.** What is the most/least it *could* be if the code were correct?
   Derive a quick invariant or domination argument.
   - Worked example: an extraction sim reported `deadlock=42/52`. But the greedy's present-set
     ⊆ baseline's present-set at every time (it exits a superset of extractable blocks), and
     baseline extracted all blocks → deadlock must be 0. The result was *theoretically
     impossible* → bug (ENTRY-before-EXIT ordering), not a finding. It was not reported.

2. **If impossible → isolate the bug with a replay/oracle check.** Replay a *known-good*
   solution (e.g. the baseline's own operations) through your harness; it must reproduce the
   known-good verdict. If it doesn't, the harness (not the algorithm) is wrong.

3. **If plausible → still validate** with `oracle-validate` before claiming it.

4. **Distinguish confounds.** Are you comparing runs at the *same* time budget / same
   assignment / same seed? Different budgets or assignments are not comparable; equalize them
   before concluding (a confound that actually bit this project).

## Reference
`docs/methodology.md` §3 ("버그인가 발견인가", "비교는 같은 조건에서").
