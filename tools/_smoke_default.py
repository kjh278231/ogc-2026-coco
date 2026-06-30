"""Smoke: run myalgorithm.algorithm with its REAL defaults (tests the setdefault flip).
usage: python _smoke_default.py <prob_json> <timelimit> [force_lahc_off] [force_portfolio]
  force_lahc_off=1 -> set SOLVER_LAHC=0 (greedy baseline for comparison)
  force_portfolio=0/1 -> override portfolio gate (default: leave to myalgorithm)

NOTE: the work MUST run under `if __name__ == "__main__"`. The portfolio uses
multiprocessing spawn, which re-imports the entry module in each child; without this
guard the module-level call would re-run in every child -> recursive spawning, the
workers never start (gather sees 0), and the run silently degrades to a tiny-budget
single-process fallback (looked like "portfolio is 4x worse" -- it was this artifact).
"""
import json, os, sys, time


def main():
    prob_path, tl = sys.argv[1], float(sys.argv[2])
    if len(sys.argv) > 3 and sys.argv[3] == "1":
        os.environ["SOLVER_LAHC"] = "0"          # force greedy
    if len(sys.argv) > 4:
        os.environ["SOLVER_PORTFOLIO"] = sys.argv[4]

    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bridge"))
    import myalgorithm, utils

    prob = json.load(open(prob_path, encoding="utf-8"))
    name = os.path.splitext(os.path.basename(prob_path))[0]
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, timelimit=tl)
    sec = time.time() - t0
    chk = utils.check_feasibility(prob, sol)
    print("SMOKE %s lahc=%s portfolio=%s obj=%.0f feasible=%s %.1fs" % (
        name, os.environ.get("SOLVER_LAHC", "(default=1)"),
        os.environ.get("SOLVER_PORTFOLIO", "(default)"),
        chk.get("objective", -1), chk.get("feasible"), sec), flush=True)


if __name__ == "__main__":
    main()
