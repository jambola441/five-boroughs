#!/usr/bin/env python3
"""The lead's interim check for r14x-1 (2026-11-22), outside the round's frozen code: count G0 rows that match a frame
site by EXACT name, before Hydrogen can see the frames.

usage: PIPELINE_RUNS=... python3 -I pipeline/tools/g0_exact_watch.py RUN_NAME [seconds between checks, default 5]
Waits until the run's frames folder has guards_frames.json, then:
- writes <run>/G0_EXACT_MATCH.txt: "<n>/<N> rows matched by exact site name" plus any site without an exact row;
- 13/13 (N/N): writes EXACT_OK in the frames folder (macmini drops READY only after it);
- otherwise: raises a queue item for the lead and writes no EXACT_OK. Exits after one verdict per file version.
Not imported by any round.
"""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decisions as Dq

RUNS = os.environ["PIPELINE_RUNS"]
ROOT = os.environ.get("NYC_ROOT", os.path.expanduser("~/Desktop/nyc-buildings"))


def main():
    run, gap = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5
    rd = os.path.join(RUNS, run)
    cfg = json.load(open(os.path.join(rd, "round.json")))
    fr = cfg["frames"].get("folder") or f"highways/work/census/frames/fdr_{cfg['round']}"
    folder = os.path.join(ROOT, fr)
    sites = [s["name"] for s in cfg["frames"].get("sites", [])]
    f = os.path.join(folder, "guards_frames.json")
    while True:
        if os.path.exists(f):
            try:
                raw = open(f, "rb").read()
                pairs = json.loads(raw).get("pairs", [])
                break
            except ValueError:   # still being written
                pass
        time.sleep(gap)
    poses = [str(x.get("pose", "")) for x in pairs]
    exact = [s for s in sites if s in poses]
    missing = [s for s in sites if s not in poses]
    extra = [p for p in poses if p not in sites]
    line = f"{len(exact)}/{len(sites)} rows matched by exact site name"
    rep = (line + (f"; sites without an exact row: {', '.join(missing)}" if missing else "")
           + (f"; rows not named exactly as a site: {', '.join(extra[:10])}" if extra else "")
           + f"\nfile sha {hashlib.sha256(raw).hexdigest()[:12]}, checked {time.strftime('%Y-%m-%d %H:%M:%S')} (the lead's interim check, outside the round's code)\n")
    open(os.path.join(rd, "G0_EXACT_MATCH.txt"), "w").write(rep)
    if not missing and len(exact) == len(sites):
        open(os.path.join(folder, "EXACT_OK"), "w").write(rep)
        print("OK", line)
    else:
        Dq.raise_item(run, "g0_exact_match", "task", "lead",
                      f"{cfg.get('round', run)}: G0 rows not matched by exact site name ({line}); decide before READY/Hydrogen",
                      {"report": rep, "frames": fr}, rd, priority=1)
        print("LEAD", line)


if __name__ == "__main__":
    main()
