"""Exp A (H-crane / separability): fix baseline's placements + entry times,
re-optimize ONLY exit timing with a greedy crane-feasible extraction sim.

Per bay, simulate time forward. At each integer t, repeatedly EXIT any present
block that (a) finished processing (t >= entry+proc) and (b) is not trapped by
currently-present blocks (utils.check_exit feasible), until a fixpoint. A block
that is done but trapped must wait -> crane-induced tardiness. If a block never
exits within the horizon, it is a cyclic-trap DEADLOCK (placement-extraction
coupling in its hardest form).

Compare T_exit_opt (placement fixed, exits greedily optimal) to T_baseline
(baseline's own joint exits). Gap tells us whether tardiness is baked into the
placement (coupling) or recoverable by extraction scheduling (separable).
"""
import sys, os, json, time, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline"))
import baseline_greedy, utils  # noqa
from utils import Bay, Block, check_exit


def extract_sim(prob, placements, j):
    """placements: dict block_id -> (x,y,orient,entry). All assigned to bay j.
    Returns: exit_time per block, n_stall (done-but-trapped events), deadlocks."""
    bays = prob["bays"]; blocks = prob["blocks"]
    bay = Bay.from_dict(bays[j], j)
    ids = list(placements.keys())
    blkobj = {i: Block(block_id=i, block_data=blocks[i],
                       x=int(round(placements[i][0])), y=int(round(placements[i][1])),
                       orient_idx=placements[i][2]) for i in ids}
    entry = {i: placements[i][3] for i in ids}
    proc = {i: blocks[i]["processing_time"] for i in ids}
    due = {i: blocks[i]["due_date"] for i in ids}
    horizon = max(entry[i] + proc[i] for i in ids) + len(ids) + 5

    present = set()
    exited = {}
    pending_entry = sorted(ids, key=lambda i: entry[i])
    pe = 0
    n_stall = 0
    for t in range(0, horizon + 1):
        # EXIT phase FIRST (Stage-5 order): fixpoint-extract done + untrapped
        changed = True
        while changed:
            changed = False
            for i in list(present):
                if t >= entry[i] + proc[i]:  # processing done
                    present_objs = [blkobj[k] for k in present]
                    if not check_exit(bay, present_objs, blkobj[i]):
                        exited[i] = t
                        present.discard(i)
                        changed = True
        # count done-but-still-present (trapped) at this t
        for i in present:
            if t >= entry[i] + proc[i]:
                n_stall += 1
        # ENTRY phase AFTER exits (a block with proc>=2 never exits on its entry day)
        while pe < len(pending_entry) and entry[pending_entry[pe]] <= t:
            present.add(pending_entry[pe]); pe += 1
        if not present and pe >= len(pending_entry):
            break
    deadlocks = [i for i in ids if i not in exited]
    for i in deadlocks:
        exited[i] = horizon  # treat as very late
    tard = sum(max(0.0, exited[i] - due[i]) for i in ids)
    return tard, n_stall, len(deadlocks)


def run(path, timelimit):
    prob = json.load(open(path, encoding="utf-8"))
    blocks = prob["blocks"]; m = len(prob["bays"])
    sol = baseline_greedy.greedyalgorithm(prob, timelimit)
    res = utils.check_feasibility(prob, sol)
    name = os.path.basename(path).replace(".json", "")
    if not res["feasible"]:
        print(f"{name}: baseline INFEASIBLE, skip"); return
    # reconstruct placements + baseline exits per bay
    place = {}; base_exit = {}
    for t_str, ops in sol["operations"].items():
        t = int(t_str)
        for op in ops:
            i = op["block_id"]
            if op["type"] == "ENTRY":
                place[i] = (op.get("x", 0), op.get("y", 0), op.get("orient_idx", 0), t, op["bay_id"])
            else:
                base_exit[i] = t
    for j in range(m):
        pj = {i: (v[0], v[1], v[2], v[3]) for i, v in place.items() if v[4] == j}
        if not pj:
            continue
        Tb = sum(max(0.0, base_exit[i] - blocks[i]["due_date"]) for i in pj if i in base_exit)
        To, stall, dead = extract_sim(prob, pj, j)
        flag = "COUPLED" if (Tb > 0 and To > 0.5 * Tb) else ("SEPARABLE" if Tb > 0 else "-")
        print(f"{name} bay{j}: T_base={Tb:.0f}  T_exit_opt={To:.0f}  "
              f"stalls={stall} deadlock={dead}  [{flag}]")


if __name__ == "__main__":
    tl = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    paths = []
    for p in sys.argv[2:]:
        paths += sorted(glob.glob(p))
    for p in paths:
        run(p, tl)
