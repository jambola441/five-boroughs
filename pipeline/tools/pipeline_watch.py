#!/usr/bin/env python3
"""Wait until something needs the PIPELINE ENGINEER, print it, exit. Everything else is left to the queue and board.

usage: PIPELINE_RUNS=… python3 -I pipeline/tools/pipeline_watch.py [seconds between checks, default 30]
Wakes on (first new one wins):
- an open decision-queue item owned by "pipeline" (e.g. wire an approved debt);
- a measuring step (not a gate) that failed or went uncertain for a reason other than waiting: likely an adapter bug;
- a run that finished, or whose lead gate passed.
State of what was already reported is kept in $PIPELINE_RUNS/_supervisor/pipeline_seen.json.
"""
import glob
import json
import os
import sys
import time

RUNS = os.environ["PIPELINE_RUNS"]
SEEN = os.path.join(RUNS, "_supervisor", "pipeline_seen.json")
WAIT = ("job running", "awaiting", "GB free", "deadline", "needs ")
GATES = ("g_", "gate_", "d_", "lg_gate", "lg_manifest", "code_verify", "lead_gate", "register_update", "frames_ready")


def findings():
    out = []
    q = os.path.join(RUNS, "_queue")
    for f in sorted(glob.glob(os.path.join(q, "*.json"))):
        it = json.load(open(f))
        if it.get("owner") == "pipeline" and it.get("status") == "open":
            out.append(("queue:" + it["id"], f"pipeline task: {it['question']}"))
    for st in sorted(glob.glob(os.path.join(RUNS, "*", "run", "state.json"))):
        run = st.split(os.sep)[-3]
        if run.startswith("_") or os.path.exists(os.path.join(RUNS, run, "RETIRED")):
            continue
        try:
            T = json.load(open(st))["tasks"]
        except (OSError, ValueError):
            continue
        for k, t in T.items():
            e = t.get("error") or ""
            if (t.get("status") in ("failed", "uncertain") and not k.startswith(GATES)
                    and not any(w in e for w in WAIT) and "validator did not pass" not in e):
                out.append((f"step:{run}:{k}:{t.get('attempt')}", f"{run}: measuring step {k} {t['status']}: {e[:200]}"))
        if T and all(t.get("status") == "verified" for t in T.values()):
            out.append((f"done:{run}", f"{run}: finished"))
        elif T.get("lead_gate", {}).get("status") == "verified":
            out.append((f"approved:{run}", f"{run}: lead gate passed"))
    return out


if __name__ == "__main__":
    gap = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    seen = set(json.load(open(SEEN))) if os.path.exists(SEEN) else {k for k, _ in findings()}   # first run: baseline
    json.dump(sorted(seen), open(SEEN, "w"))
    while True:
        new = [(k, m) for k, m in findings() if k not in seen]
        if new:
            for _, m in new:
                print(m)
            json.dump(sorted(seen | {k for k, _ in new}), open(SEEN, "w"))
            break
        time.sleep(gap)
