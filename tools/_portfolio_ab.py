"""Wall-mode portfolio A/B: run myalgorithm.algorithm (portfolio path, T>=180) from a
GIVEN code dir, so old (shipped zip) vs new (current bridge/) portfolios are compared on
the real shipped path. MUST be __main__-guarded: the portfolio uses multiprocessing spawn,
which re-imports this module in every child -- without the guard each child re-runs
algorithm() -> recursive spawn chaos (memory/portfolio-spawn-guard).

usage: python _portfolio_ab.py <code_dir> <prob_json> <timelimit> <tag>
prints: RESULT <name> <tag> <obj> <feasible> <sec>
"""
import json, os, sys, time


def main():
    code_dir, prob_path, tl, tag = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
    sys.path.insert(0, os.path.abspath(code_dir))
    import myalgorithm, utils
    prob = json.load(open(prob_path, encoding="utf-8"))
    name = os.path.splitext(os.path.basename(prob_path))[0]
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, timelimit=tl)
    sec = time.time() - t0
    chk = utils.check_feasibility(prob, sol)
    print("RESULT %s %s %.0f %s %.1fs" % (
        name, tag, chk.get("objective", -1), chk.get("feasible"), sec), flush=True)


if __name__ == "__main__":
    main()
