"""Standalone smoke of a packaged submission: argv = <extracted_dir> <prob_json> <timelimit>.
Paths come via argv so Git Bash mangles them to Windows paths correctly. Guarded under
__main__ so the portfolio's spawn workers don't re-run this module."""
import sys, json, time


def main():
    zipdir, prob_path, tl = sys.argv[1], sys.argv[2], float(sys.argv[3])
    sys.path.insert(0, zipdir)
    import myalgorithm, utils
    prob = json.load(open(prob_path, encoding="utf-8"))
    t0 = time.time()
    sol = myalgorithm.algorithm(prob, timelimit=tl)
    sec = time.time() - t0
    chk = utils.check_feasibility(prob, sol)
    print("ZIP-SMOKE obj=%.0f feasible=%s %.1fs" % (
        chk.get("objective", -1), chk.get("feasible"), sec), flush=True)


if __name__ == "__main__":
    main()
