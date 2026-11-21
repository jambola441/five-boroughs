#!/usr/bin/env python3
"""Round supervisor: the standing policies that move rounds along without anyone being asked (POLICY.md, 3 and 4).

usage: NYC_ROOT=… PE_ROOT=… PIPELINE_RUNS=… python3 -I pipeline/tools/supervise.py [seconds between passes, default 60]
Each pass, for every run folder with unfinished steps (and no driver of its own running):
- a step whose job is still running, or that ran out of Irrigator's time ("deadline") -> retried;
- frames_ready waiting and its folder has READY -> retried; lead_gate waiting and approval.json exists -> retried;
- a highway check that failed and whose owner has since changed its reasons file -> its jobs set aside, retried;
then `irrigator resume` runs in the background. Every action is logged to $PIPELINE_RUNS/_supervisor/events.jsonl.
It never edits round files, never approves, never deletes: it only retries and resumes.
"""
import glob
import json
import os
import subprocess
import sys
import time

RUNS = os.environ["PIPELINE_RUNS"]
ROOT = os.environ["NYC_ROOT"]
PE = os.environ.get("PE_ROOT", "")
LOG = os.path.join(RUNS, "_supervisor")
os.makedirs(LOG, exist_ok=True)
REASONS = {"d_tile_inventory": "tile_inventory_reasons.json", "d_walls": "wall_near_lane_reasons.json",
           "d_void": "void_diff_reasons.json"}   # macmini's reasons files, in highways/work/p2net
HW = os.path.join(ROOT, "highways/work/p2net")


def event(run, what, **kw):
    with open(os.path.join(LOG, "events.jsonl"), "a") as f:
        f.write(json.dumps({"at": time.strftime("%H:%M:%S"), "run": run, "what": what, **kw}) + "\n")


def busy(run_dir):
    pat = run_dir.rstrip("/") + "/run"
    for p in (f"irrigator resume {pat}", f"drive_run.sh {pat}"):
        if subprocess.run(["pgrep", "-f", p], capture_output=True).returncode == 0:
            return True
    return False


def resolve(rel):
    if rel.startswith("pe:"):
        return os.path.join(PE, rel[3:])
    if rel.startswith("runs:"):
        return os.path.join(RUNS, rel[5:])
    return os.path.join(ROOT, rel)


def job_done_at(run_dir, prefix):
    rcs = glob.glob(os.path.join(run_dir, "jobs", prefix + "*", "rc"))
    return max((os.path.getmtime(p) for p in rcs), default=0)


def plan(run_dir):
    st = json.load(open(os.path.join(run_dir, "run", "state.json")))
    cfg = json.load(open(os.path.join(run_dir, "round.json")))
    out = []   # (step, flags, why, reset_jobs)
    for tid, t in st["tasks"].items():
        s, err = t.get("status"), t.get("error") or ""
        if s == "failed" and ("job running" in err or "GB free" in err):
            out.append((tid, [], "job running", False))
        elif s == "uncertain" and "deadline" in err:
            out.append((tid, ["--allow-uncertain"], "ran out of time; its job is idempotent", False))
        elif s == "failed" and tid == "frames_ready" and err.startswith("awaiting frames"):
            f = (cfg.get("frames") or {}).get("folder")
            if f and os.path.exists(os.path.join(resolve(f), "READY")):
                out.append((tid, [], "frames READY", False))
        elif s == "failed" and tid == "lead_gate" and os.path.exists(os.path.join(run_dir, "approval.json")):
            out.append((tid, [], "approval.json present", False))
        elif s == "failed" and tid in REASONS:
            rf = os.path.join(HW, REASONS[tid])
            if os.path.exists(rf) and os.path.getmtime(rf) > job_done_at(run_dir, tid):
                out.append((tid, [], f"{REASONS[tid]} changed since the check ran", True))
    return out


def unfinished(run_dir):
    try:
        st = json.load(open(os.path.join(run_dir, "run", "state.json")))
    except (OSError, ValueError):
        return False
    return any(t.get("status") != "verified" for t in st["tasks"].values())


def running_jobs(run_dir):
    return any(not os.path.exists(os.path.join(d, "rc")) for d in glob.glob(os.path.join(run_dir, "jobs", "*"))
               if os.path.exists(os.path.join(d, "job.json")))


def summary(run_dir):
    """stopped / waiting on people / finished / moving: what a person would want to hear about."""
    st = json.load(open(os.path.join(run_dir, "run", "state.json")))
    T = st["tasks"]
    if all(t.get("status") == "verified" for t in T.values()):
        return "finished"
    hard = [k for k, t in T.items() if t.get("status") in ("failed", "uncertain")
            and not any(w in (t.get("error") or "") for w in ("job running", "awaiting", "GB free", "deadline"))]
    if hard:
        return "stopped at " + ", ".join(sorted(hard))
    if "lead_gate" in T and "awaiting lead approval" in (T["lead_gate"].get("error") or ""):
        return "waiting on the lead"
    if "frames_ready" in T and "awaiting frames" in (T["frames_ready"].get("error") or ""):
        return "waiting on frames"
    return "moving"


def report_changes():
    path = os.path.join(LOG, "status.json")
    old = json.load(open(path)) if os.path.exists(path) else {}
    new = {}
    for run_dir in sorted(glob.glob(os.path.join(RUNS, "*"))):
        name = os.path.basename(run_dir)
        if name.startswith("_") or os.path.exists(os.path.join(run_dir, "RETIRED")) \
                or not os.path.exists(os.path.join(run_dir, "run", "state.json")):
            continue
        try:
            new[name] = summary(run_dir)
        except (OSError, ValueError, KeyError):
            continue
        if old and new[name] != old.get(name) and new[name] != "moving":
            event(name, "status", status=new[name])
    json.dump(new, open(path, "w"), indent=0)


def tick():
    report_changes()
    for run_dir in sorted(glob.glob(os.path.join(RUNS, "*"))):
        name = os.path.basename(run_dir)
        if (name.startswith("_") or not os.path.exists(os.path.join(run_dir, "round.json"))
                or os.path.exists(os.path.join(run_dir, "RETIRED"))):   # dropped or superseded runs stay put
            continue
        if not unfinished(run_dir) or busy(run_dir):
            continue
        todo = plan(run_dir)
        if not todo:
            continue
        env = {**os.environ, "PIPELINE_OUT": run_dir}
        for tid, flags, why, reset in todo:
            if reset:
                for d in glob.glob(os.path.join(run_dir, "jobs", tid + "*")):
                    if not d.endswith(".set_aside") and ".before" not in d:
                        os.rename(d, d + f".set_aside_{int(time.time())}")
            r = subprocess.run(["irrigator", "retry", *flags, os.path.join(run_dir, "run"), tid],
                               capture_output=True, text=True, env=env)
            event(name, "retry", step=tid, why=why, ok=r.returncode == 0, out=(r.stdout + r.stderr).strip()[-200:])
        with open(os.path.join(LOG, f"resume_{name}.txt"), "w") as f:
            subprocess.Popen(["irrigator", "resume", os.path.join(run_dir, "run")], stdout=f, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
        event(name, "resume", steps=[t[0] for t in todo])


if __name__ == "__main__":
    gap = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    event("-", "start", gap=gap)
    while True:
        try:
            tick()
        except Exception as e:   # a run mid-write: next pass
            event("-", "error", error=f"{type(e).__name__}: {e}"[:300])
        time.sleep(gap)
