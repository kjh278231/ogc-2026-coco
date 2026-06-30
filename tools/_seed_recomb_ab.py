"""One (instance, config) ALNS run in a FRESH process -> avoids id()-keyed cache
contamination across instances (see memory/perf-ab-one-process-per-instance).

usage: python _seed_recomb_ab.py <prob_json> <A|B> <timelimit>
  A = current submission default (no levers).
  B = ALNS_MIP_SEED=1 + ALNS_RECOMB=final (seed + final recombine ON).
prints one line: RESULT <name> <cfg> <obj> <feasible> <mip_seed> <recomb_att> <recomb_win> <sec>
"""
import json, os, sys, time

prob_path, cfg, tl = sys.argv[1], sys.argv[2], float(sys.argv[3])
if cfg == "B":
    os.environ["ALNS_MIP_SEED"] = "1"
    os.environ["ALNS_RECOMB"] = "final"

HERE = os.path.dirname(os.path.abspath(__file__))
ALNS = os.path.join(os.path.dirname(HERE), "ALNS")
sys.path.insert(0, ALNS)

import myalgorithm, alns, utils

prob = json.load(open(prob_path, encoding="utf-8"))
name = os.path.splitext(os.path.basename(prob_path))[0]
t0 = time.time()
res = myalgorithm.algorithm(prob, timelimit=tl)
sec = time.time() - t0
chk = utils.check_feasibility(prob, res)
st = alns.LAST_STATS
print("RESULT %s %s %.0f %s mip_seed=%s recomb_att=%s recomb_win=%s %.1fs" % (
    name, cfg, chk.get("objective", -1), chk.get("feasible"),
    st.get("mip_seed"), st.get("recomb_attempts"), st.get("recomb_wins"), sec), flush=True)
