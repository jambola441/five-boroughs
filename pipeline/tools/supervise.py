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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


STREAM_OWNER = {"page_round": "page-coordinator", "terrain_round_live": "lead", "highway_round": "macmini",
                "research_swarm": "stations-coordinator"}


def sync_queue(run_dir):
    """POLICY.md's pull queue: raise what this run waits on, apply answers, close what cleared."""
    import decisions as Dq
    name = os.path.basename(run_dir)
    st = json.load(open(os.path.join(run_dir, "run", "state.json")))
    cfg = json.load(open(os.path.join(run_dir, "round.json")))
    T = st["tasks"]
    res = lambda k: {a: str(b)[:600] for a, b in ((T.get(k) or {}).get("result") or {}).items() if a not in ("out", "rows")}
    hard = sorted(k for k, t in T.items() if t.get("status") in ("failed", "uncertain")
                  and not any(w in (t.get("error") or "") for w in ("job running", "awaiting", "GB free", "deadline")))
    lead_wait = "lead_gate" in T and "awaiting lead approval" in (T["lead_gate"].get("error") or "")
    fr = (cfg.get("frames") or {}).get("folder")
    frames_wait = ("frames_ready" in T and "awaiting frames" in (T["frames_ready"].get("error") or "")
                   and not (fr and os.path.exists(os.path.join(resolve(fr), "READY"))))
    if lead_wait and not os.path.exists(os.path.join(run_dir, "approval.json")):
        acc = next((k for k in ("page_accept", "qa_accept", "highway_accept", "explore_accept") if k in T), None)
        Dq.raise_item(name, "lead_gate", "approval", "lead", f"Approve {cfg.get('round', name)}?",
                      {"accept": res(acc) if acc else {}, "debt": cfg.get("debt", []) + cfg.get("qa_debt", []),
                       "changes": cfg.get("changes", [])}, run_dir, priority=1)
    if frames_wait:
        Dq.raise_item(name, "frames_ready", "frames", "page-editor",
                      f"Render the frames for {cfg.get('round', name)} into {fr}, then drop READY",
                      {"request": os.path.join(run_dir, "frames_request.json")}, run_dir, priority=2)
    if hard:
        owner = STREAM_OWNER.get(cfg.get("kind"), "lead")
        key = "+".join(hard)
        for it in Dq.items():   # one open verdict per run: a newer set of stopped steps replaces the older item
            if it["run"] == name and it["kind"] == "verdict" and it["status"] == "open" and it["step"] != key:
                Dq.close(it, f"superseded: the run now stops at {', '.join(hard)}")
        # the lead: a failed condition of a conditional debt (a hydrogen_pose gate) fails the round, no debt offered
        nodebt = [g["id"] for g in cfg.get("gates", []) if g["id"] in hard and g.get("check") == "hydrogen_pose"]
        ch = [c for c in Dq.CHOICES["verdict"] if not (nodebt and c == "debt")]
        Dq.raise_item(name, "+".join(hard), "verdict", owner,
                      f"{cfg.get('round', name)} stopped at {', '.join(hard)}: {', '.join(ch)}?"
                      + (f" (no debt: {', '.join(nodebt)} is the lead's condition)" if nodebt else ""),
                      {k: {**res(k), "error": (T[k].get("error") or "")[:300]} for k in hard}, run_dir, priority=1,
                      choices=ch)
    rows = os.path.join(run_dir, "register_rows_proposed.jsonl")
    if T.get("register_update", {}).get("status") == "verified" and os.path.exists(rows):
        last = json.loads(open(rows).read().strip().splitlines()[-1])
        Dq.raise_item(name, "register_update", "task", "lead",
                      f"Write {cfg.get('round', name)}'s register row (proposed by the graph)",
                      {"row_file": rows, "decision": last.get("decision"), "bundle": last.get("bundle"),
                       "debt": last.get("debt"), "lead_note": last.get("lead_note")}, run_dir, priority=3)
    for it in Dq.items():
        if it["run"] != name or it["status"] not in ("open", "answered"):
            continue
        k, steps = it["kind"], it["step"].split("+")
        if it["status"] == "open":   # cleared on its own?
            if k == "approval" and T.get("lead_gate", {}).get("status") == "verified":
                Dq.close(it, "the lead gate passed")
            elif k == "frames" and fr and os.path.exists(os.path.join(resolve(fr), "READY")):
                Dq.close(it, "frames READY")
            elif k == "verdict" and not any(T.get(x, {}).get("status") in ("failed", "uncertain") for x in steps):
                Dq.close(it, "the stopped steps passed (e.g. after reasons were added)")
            continue
        if it.get("applied"):
            continue
        if it["version"] != Dq.version(run_dir):   # never apply an answer to a round that changed under it
            it["status"], it["closed_why"] = "stale", "the round changed after the item was raised"
            Dq.save(it)
            event(name, "queue", item=it["id"], change="stale")
            continue
        a = it["answer"]
        note = f"{a['by']}: {a.get('note', '')}".strip()
        if k == "approval" and a["choice"] == "approve":
            json.dump({"decision": "approve", "by": a["by"], "note": a.get("note", ""), "via": it["id"]},
                      open(os.path.join(run_dir, "approval.json"), "w"))
        elif (k == "approval" and a["choice"] == "reject") or (k == "verdict" and a["choice"] == "fix"):
            open(os.path.join(run_dir, "RETIRED"), "w").write(f"{a['choice']} via the decision queue ({note})\n")
        elif k == "verdict" and a["choice"] == "debt":
            Dq.raise_item(name, it["step"], "debt", "lead", f"Put {cfg.get('round', name)}'s stop at {it['step']} "
                          f"on the debt list? ({note})", {**it["evidence"], "request": note}, run_dir, priority=1)
        elif k == "debt" and a["choice"] == "approve":
            Dq.raise_item(name, it["step"], "task", "pipeline", f"Wire the lead's debt for {name} at {it['step']}",
                          {**it["evidence"], "lead": note}, run_dir, priority=1)
        it["applied"] = time.strftime("%H:%M:%S")
        if k != "approval":
            it["status"], it["closed_why"] = "closed", f"applied: {a['choice']}"
        Dq.save(it)
        event(name, "queue", item=it["id"], applied=a["choice"], by=a["by"])


def close_retired():
    """A retired run waits on nobody: its open items close."""
    import decisions as Dq
    for it in Dq.items():
        if it["status"] == "open" and os.path.exists(os.path.join(RUNS, it["run"], "RETIRED")):
            Dq.close(it, "the run was retired")
            event(it["run"], "queue", item=it["id"], change="closed: run retired")


def tick():
    report_changes()
    close_retired()
    for run_dir in sorted(glob.glob(os.path.join(RUNS, "*"))):
        if (os.path.basename(run_dir).startswith("_") or os.path.exists(os.path.join(run_dir, "RETIRED"))
                or not os.path.exists(os.path.join(run_dir, "run", "state.json"))):
            continue
        try:
            sync_queue(run_dir)
        except (OSError, ValueError, KeyError) as e:
            event(os.path.basename(run_dir), "error", error=f"queue: {type(e).__name__}: {e}"[:300])
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
            cfg = json.load(open(os.path.join(run_dir, "round.json")))   # a swarm's per-call budget and lane count
            extra = (["--call-budget", str(cfg["call_budget_usd"])] if cfg.get("call_budget_usd") else []) + \
                    (["--workers", str(cfg["workers"])] if cfg.get("workers") else [])
            subprocess.Popen(["irrigator", "resume", *extra, os.path.join(run_dir, "run")], stdout=f, stderr=subprocess.STDOUT,
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
