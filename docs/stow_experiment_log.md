# STOW Experiment Log (fourth algorithm — placement-centric)

Separate from BRIDGE (`docs/experiment_log.md`), PRISM (`docs/prism_experiment_log.md`),
and the older covering prototype. STOW is the user's 2026-07-01 goal: **a genuinely new
technique in a new folder, focused on improving 적치 (placement/stowage)**, while BRIDGE/PRISM
keep being tuned.

Entry point (planned): `stow/myalgorithm.py` -> `stow/stow_engine.py:stow_solve`. Like PRISM,
STOW reuses ONLY the validated packing/scoring/search kernel from `bridge/` (imported as `K`);
the new part is the orchestration.

## The gap STOW attacks (code- and data-verified, see [[placement-lever-diagnosis]])

Every existing solver (BRIDGE / PRISM / ALNS) shares ONE deterministic per-bay packer
(`solve_bay`: fixed EDD order, bottom-left first-fit, union-footprint-disjoint). The search
only ever varies the ASSIGNMENT; the PACKER is a fixed black box. Yet:

- Tardiness Z1 is 100% contention (temporal floor = 0 on every train instance — no block is
  late at its release; it is the packer pushing entries late). So the packer directly owns Z1.
- The packing ORDER is a real lever: bay-oracle over {EDD, release, least-slack, area-desc}
  cuts a_pref Z1 by **−24%** on the hard family; `release` alone −19% (`tools/_order_probe.py`).
- Blocks are NOT boxes (82–98% multilayer, 34–57% stepped, AABB fill 56–72%), so the crane
  model permits **interlocking** (an overhang layer over a shorter neighbour's floor) that the
  union-disjoint packer forbids entirely — an untouched capacity lever for the saturated bays.

**STOW's distinct idea: make the PACKER the diversity axis of a parallel portfolio.** Each
worker runs the assignment search against a DIFFERENT packing policy (EDD-only / multi-order /
release-first / … / eventually interlocking), and the master takes the best by TRUE score.
This is orthogonal to BRIDGE (workers differ by search seed) and PRISM (workers differ by MIP
anchor). It is the natural — and Pareto-safe — way to harvest the multi-order lever, whose
raw form is a strong but instance-split lever (below).

## Multi-order best-of packer (the v0 kernel, `SOLVER_MULTIORDER`)

Implemented in `bridge/packing.py` (`solve_bay(order=)` + `solve_bay_best`) and wired through
`bridge/solver.py` (`_MULTIORDER`, in eval_obj1 + _bestof_obj + _score_and_pack, so search,
guard, and build stay consistent — no proxy seam). Default OFF = bit-identical. `solve_bay_best`
packs EDD first and, only on a tardy bay, also packs the alternative orders and keeps the
min-tardiness placement (EDD always in the set → per-bay Pareto-safe).

**Wall T=60 A/B (single-process, true-scored), OFF vs ON:**

| inst | OFF | ON | Δ |
|------|----:|---:|---:|
| T1  | 181,326 | 15,611  | **−91%** (the known deterministic trap) |
| T9  | 170,891 | 82,460  | **−52%** |
| T11 | 435,835 | 227,891 | **−48%** |
| T2  | 4,970   | 3,690   | **−26%** |
| T6  | 371,167 | 406,708 | +9.6% (regression) |
| T18 | 499,450 | 603,557 | +21% (regression; ON drifts to a tardy basin) |
| T13/T14/T20 | … | … | (heavies — pending) |

Read: at a fixed eval count multi-order is a near-Pareto packing win (T2/T11/T9 −26..−48% @
E=1500), but in WALL mode it changes the SEARCH TRAJECTORY (a lower Z1 estimate shifts move
acceptance), which is a big net win on most instances yet drifts to a worse basin on a few
(T6/T18) — the same instance-split as guided-destroy / MIP-seed / LAHC-diverse
([[guided-destroy-portfolio]], [[alns-seed-recombine-instance-split]]). So multi-order is a
**portfolio/best-of lever, not a blanket default** — exactly STOW's structure.

## Plan

- **v0**: `stow/` = bridge-kernel + a packing-diverse portfolio (some multi-order workers, some
  single-order/EDD), best-of by true `_score_and_pack`. Gate like the others (T≥ some floor).
  Validate vs BRIDGE/PRISM portfolios on wall (true obj), then grader ([[anchor-to-grader-best]]).
- **Confirm regressions are deterministic** (eval-count A/B on T6/T18) vs wall noise
  ([[eval-count-ab-protocol]] — wall varies 20–66%); decide whether the EDD worker alone guards them.
- **Back-port to BRIDGE/PRISM** (goal #2): add ONE multi-order worker to their portfolios
  (best-of guards the regressions) — minimal, since both already inherit `_MULTIORDER` via the
  shared kernel (PRISM calls `K.total_obj`/`K._score_and_pack`).
- ~~**Frontier — interlocking**~~ **DE-PRIORITISED (ceiling falsified, `tools/_interlock_ceiling.py`).**
  Optimistic ceiling = pack to floor-layer area instead of union area (ignores the crane sweep, so an
  upper bound). Result: the saturated bays that dominate Z1 stay saturated even on floor area (T20-bay1
  union_util 1.75 → floor 1.56; T6-bay0 1.27 → 1.11) — their over-subscription is fundamental floor
  demand, not overhang the union-packer wastes. floor_util drops only ~8–13% on the rest (and the packer
  already picks compact, low-overhang orientations, so the real gain is smaller). Only marginal bays
  de-saturate (T14-bay3 1.23→0.94, T11-bay0 1.04→0.94). So a feasibility-critical per-layer+crane packer
  buys little beyond the order lever and is not worth its risk. The Z1 levers are ORDER (validated) and
  ASSIGNMENT (the existing search owns saturated bays). [[bitmask-collision-result]]

## 3-way portfolio A/B (T=180, true grader obj) — STOW VALIDATED, beats both on aggregate

`tools/_solver3_ab.sh` -> `.claude/scratch/_solver3_T180.txt`. bridge (seed-diverse) vs prism
(anchor-diverse) vs stow (packing-diverse), 6 hard instances, interleaved (wall-noise controlled).

| inst | bridge | prism | **stow** | winner |
|------|-------:|------:|---------:|--------|
| T1  | 41,024  | 32,188  | **15,611**  | STOW −52% vs prism (multi-order escapes the trap) |
| T9  | 62,595  | 69,360  | **51,805**  | STOW −17% / −25% |
| T11 | 51,282  | **38,262** | 51,282  | PRISM (STOW == bridge; STOW lacks the MIP anchor that wins T11) |
| T13 | 175,818 | 168,579 | **131,606** | STOW −22% vs prism |
| T18 | **68,433** | 72,839 | 78,656  | BRIDGE (STOW worst; sacrificed bridge's L30/greedy diversity) |
| T20 | 266,393 | 223,903 | **221,532** | STOW (≈ prism) |
| **sum** | 665,545 | 605,131 | **550,292** | **STOW −9% vs prism, −17% vs bridge** |

**Verdict: STOW (packing-diverse portfolio) is a validated new technique — best aggregate, wins
4/6 hard instances, including big wins on the trap (T1) and the Z3-heavy family (T13/T9).** The
packing-diversity (multi-order workers) clearly pays off where it wins. The two losses are
coverage gaps from the 4-core cap: T11 wants PRISM's MIP anchor (STOW has none → falls back to
the bridge-level 51,282), T18 wants bridge's L30/greedy worker (STOW dropped them for multi-order).

**Key implication:** multi-order is a KERNEL lever, so the strongest solver likely COMBINES the
levers — PRISM's MIP anchors (T11) + multi-order packing (T1/T9/T13) + some bridge diversity
(T18). The cheapest test of that is **PRISM + multi-order** (PRISM already inherits SOLVER_MULTIORDER
via the shared kernel): it keeps the MIP-anchor workers that win T11 and adds the multi-order
packing that wins the rest. If PRISM+MO >= STOW everywhere, the deliverable is "enable multi-order
in PRISM" (goal #2) and STOW is the concept's proof. Testing next.

## Open questions
- Deterministic size of the multi-order wins/regressions (eval-count, all 20).
- Interlocking ceiling (is there enough overhang to justify the feasibility-critical packer?).
- Wall cost in the portfolio (multi-order is ~2–3× per-eval on tardy bays at fixed E; in wall
  mode it trades evals for per-eval quality — measure per-worker convergence at T≥180).
