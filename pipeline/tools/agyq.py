#!/usr/bin/python3 -I
"""agy behind a machine-wide queue (owner, 2026-11-22: "put a proxy script in between where queue up calls").

Install: a folder holding a link named agy to this file, first on PATH for pipeline runs (pipeline/tools/agyq/agy,
untracked). Then every agy call then waits (blocking, no polling) for one of AGY_SLOTS
slots (default 8) before the real agy starts, so any number of ready steps can start their jobs without overloading the
machine. Slots are flock'd files in ~/.cache/agyq/; a crashed call releases its slot automatically. The real binary is
AGY_REAL (default ~/.local/bin/agy). Waiting time is printed to stderr; arguments and stdout pass through unchanged.
"""
import fcntl
import os
import sys
import time

real = os.environ.get("AGY_REAL") or os.path.expanduser("~/.local/bin/agy")
slots = max(1, int(os.environ.get("AGY_SLOTS", "8")))
d = os.path.expanduser("~/.cache/agyq")
os.makedirs(d, exist_ok=True)
t0 = time.time()
while True:
    for i in range(slots):
        f = open(os.path.join(d, f"slot{i}.lock"), "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            continue
        f.write(f"{os.getpid()} {time.strftime('%H:%M:%S')}\n")
        f.flush()
        os.set_inheritable(f.fileno(), True)   # the lock stays held by the exec'd agy until it exits
        waited = time.time() - t0
        if waited > 1:
            print(f"agyq: waited {waited:.0f}s for slot {i}/{slots}", file=sys.stderr)
        os.execv(real, [real] + sys.argv[1:])
    time.sleep(2)
