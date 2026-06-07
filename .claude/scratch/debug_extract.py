"""Debug: replay baseline's EXACT exit schedule through our present-tracking +
check_exit. Since baseline is feasible, every replayed exit MUST be obstruction-
free and tardiness MUST match. If not, our harness/check_exit usage is buggy.
Also processes EXIT-before-ENTRY at each time point (Stage-5 order).
"""
import sys, os, json
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "baseline"))
import baseline_greedy, utils
from utils import Bay, Block, check_exit

prob = json.load(open("train/prob_1.json", encoding="utf-8"))
sol = baseline_greedy.greedyalgorithm(prob, 30)
res = utils.check_feasibility(prob, sol)
print("baseline feasible:", res["feasible"], "obj1:", res["obj1"])

blocks = prob["blocks"]
# per bay, replay
place = {}; exits = {}
for t_str, ops in sol["operations"].items():
    t = int(t_str)
    for op in ops:
        i = op["block_id"]
        if op["type"] == "ENTRY":
            place[i] = (op.get("x", 0), op.get("y", 0), op.get("orient_idx", 0), t, op["bay_id"])
        else:
            exits[i] = t

for j in range(len(prob["bays"])):
    ids = [i for i in place if place[i][4] == j]
    bay = Bay.from_dict(prob["bays"][j], j)
    blk = {i: Block(block_id=i, block_data=blocks[i],
                    x=int(round(place[i][0])), y=int(round(place[i][1])),
                    orient_idx=place[i][2]) for i in ids}
    entry = {i: place[i][3] for i in ids}
    # event replay in Stage-5 order: at each t, EXIT first then ENTRY
    times = sorted(set([entry[i] for i in ids] + [exits[i] for i in ids]))
    present = set()
    bad = 0
    for t in times:
        for i in ids:
            if exits[i] == t:
                if i not in present:
                    continue
                pobjs = [blk[k] for k in present]
                if check_exit(bay, pobjs, blk[i]):
                    bad += 1
                present.discard(i)
        for i in ids:
            if entry[i] == t:
                present.add(i)
    print(f"bay{j}: n={len(ids)} replay baseline exits -> obstructed_count={bad} "
          f"(expect 0); leftover_present={len(present)}")
