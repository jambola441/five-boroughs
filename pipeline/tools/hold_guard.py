#!/usr/bin/env python3
"""Hold a run before a step if a data file isn't in the state the lead requires (not imported by any round).

usage: PIPELINE_RUNS=... python3 -I pipeline/tools/hold_guard.py RUN TRIGGER_STEP FILE [seconds, default 3]
Watches RUN: once TRIGGER_STEP (a step well before the guarded one) leaves pending, FILE must hold an empty JSON object or list
(or be missing). If it isn't, writes <run>/HOLD (the supervisor then neither retries nor resumes the run), stops
the run's controller, and exits with a line saying why. If FILE is empty first, exits with OK and does nothing.
r14t2-1 (the lead, 2026-11-22): tile_diff_reasons_r14t2.json must be empty when d_tile_diff runs.
"""
import json
import os
import subprocess
import sys
import time

RUNS = os.environ["PIPELINE_RUNS"]


def empty(f):
    if not os.path.exists(f):
        return True
    try:
        d = json.load(open(f))
    except ValueError:
        return False
    return d in ({}, [])


def main():
    run, step, f = sys.argv[1:4]
    gap = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    rd = os.path.join(RUNS, run)
    while True:
        if empty(f):
            print(f"OK {time.strftime('%H:%M:%S')}: {os.path.basename(f)} is empty")
            return
        T = json.load(open(os.path.join(rd, "run", "state.json")))["tasks"]
        if T[step]["status"] != "pending":
            why = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}: held at {step} (before the guarded step): {f} is not empty "
                   f"(the lead: it must be empty when d_tile_diff runs)")
            open(os.path.join(rd, "HOLD"), "w").write(why + "\n")
            subprocess.run(["pkill", "-f", f"irrigator (resume|apply).*{run}/"])
            print("HOLD " + why)
            return
        time.sleep(gap)


if __name__ == "__main__":
    main()
