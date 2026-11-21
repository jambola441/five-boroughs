"""Shared helpers for Irrigator command adapters (swarm.worker/v1 over stdin/stdout).

Adapters are READ-ONLY on the project: they read frozen rounds and finished reports from
NYC_ROOT and write only inside the run's own output folder (PIPELINE_OUT).
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.environ.get("NYC_ROOT", "")
OUT = os.environ.get("PIPELINE_OUT", "")


_REQ = {}


def load_request():
    _REQ.update(json.load(sys.stdin))
    return _REQ


def respond(req, outcome="settled", output=None, error=""):
    resp = {"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": outcome,
            "output": {k: str(v) for k, v in (output or {}).items()}, "cost_usd": 0}
    if error:
        resp["error"] = error
    json.dump(resp, sys.stdout)


def validate(req, passed):
    json.dump({"protocol": "swarm.worker/v1", "request_id": req["request_id"], "passed": bool(passed),
               "cost_usd": 0}, sys.stdout)


def project(rel):
    """A project path relative to NYC_ROOT; 'runs:<path>' relative to PIPELINE_RUNS (earlier runs' outputs);
    'pe:<path>' relative to PE_ROOT (the page editor's folder)."""
    if rel.startswith("pe:"):   # the page editor's folder: given only by PE_ROOT, never named in this repo
        if not os.environ.get("PE_ROOT"):
            raise RuntimeError("PE_ROOT is not set")
        return os.path.join(os.environ["PE_ROOT"], rel[len("pe:"):])
    if rel.startswith("runs:"):
        return os.path.join(os.environ.get("PIPELINE_RUNS", os.path.dirname(OUT)), rel[len("runs:"):])
    if not ROOT:
        raise RuntimeError("NYC_ROOT is not set")
    return os.path.join(ROOT, rel)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def deps(req):
    return req.get("dependencies") or {}


def round_config():
    """The round builder writes round.json into PIPELINE_OUT; every adapter reads it."""
    with open(os.path.join(OUT, "round.json")) as f:
        return json.load(f)


DEFAULT_COORDINATOR = "agy:gemini-4-hydrogen-high"  # Gemini by default, to save Claude quota


def coordinator_json(prompt, schema, cwd, spec=None, timeout=600):
    """Run one coordinator judgement on the model named by spec ('agy:MODEL' or 'claude:MODEL')."""
    spec = spec or DEFAULT_COORDINATOR
    vendor, _, model = spec.partition(":")
    if vendor == "claude":
        return claude_json(prompt, schema, cwd, model=model, timeout=timeout)
    if vendor == "agy":
        return agy_json(prompt, schema, cwd, model=model, timeout=timeout)
    raise ValueError(f"unknown coordinator {spec}")


def agy_json(prompt, schema, cwd, model="gemini-4-hydrogen-high", timeout=600):
    """One Gemini call through agy in an empty private folder, shell commands denied, JSON-schema output."""
    work = os.path.join(cwd, "agy_coord")
    os.makedirs(work, exist_ok=True)
    p = subprocess.run(["agy", "-p", prompt, "--model", model, "--add-dir", work,
                        "--settings", '{"permissions":{"deny":["command(*)"]}}', "--json-schema", json.dumps(schema),
                        "--print-timeout", f"{timeout}s", "--output-format", "json"],
                       cwd=work, capture_output=True, text=True, timeout=timeout + 60)
    data = json.loads(p.stdout)
    if data.get("status") != "SUCCESS":
        raise RuntimeError(f"agy status {data.get('status')}")
    out = data.get("structured_output")
    if out is None:
        import re
        m = re.findall(r"\{.*\}", data.get("response", ""), re.S)
        out = json.loads(m[-1]) if m else None
    if not isinstance(out, dict):
        raise RuntimeError("agy returned no JSON object")
    return out


def claude_json(prompt, schema, cwd, model="claude-opus-5-5", timeout=600):
    """One isolated Claude call: no settings, hooks, memory, tools or MCP; nothing saved.
    USER must be in the adapter's environment for the CLI to find its sign-in."""
    p = subprocess.run(["claude", "-p", "--setting-sources", "", "--model", model, "--tools", "",
                        "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
                        "--json-schema", json.dumps(schema)],
                       input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    data = json.loads(p.stdout)
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result"))[:200])
    return data.get("structured_output") or json.loads(data.get("result", "{}"))


# ---------- long steps ----------
# Irrigator ends a command step (and a validator) after 120 s and marks it uncertain ("deadline"); swarm/v1 plans
# can't raise that. So anything longer runs as a detached JOB: the first call starts it in its own session (outside
# Irrigator's process group, so it survives the step's end and an Irrigator crash) and returns not_started
# "job running"; every later call (irrigator resume RUN --retry-failed, or tools/drive_run.sh) picks the same job
# up by name and settles once it has exited. A job is started at most once per run folder.

JOB_WAIT = 90  # seconds a call waits for its job before handing back (under the 120 s deadline)
import time as _time
_CALL_START = _time.time()   # this adapter call's start: a step that chains jobs never waits past the deadline


class Running(Exception):
    """The step's job is still running: the step reports not_started and is retried later."""


def run_job(name, cmd, cwd, log, env=None, wait=None):
    """Run cmd as a detached job named name; return its exit code once it has finished, else raise Running.
    stdout+stderr go to OUT/log. A job that died without writing its exit code raises RuntimeError."""
    import time
    d = os.path.join(OUT, "jobs", name)
    rec, rcf = os.path.join(d, "job.json"), os.path.join(d, "rc")
    if not os.path.exists(rec):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(OUT, log), "w") as f:
            p = subprocess.Popen(["/bin/sh", "-c", '"$@"; echo $? > "$JOB_RC.tmp" && mv "$JOB_RC.tmp" "$JOB_RC"',
                                  "job"] + list(cmd), cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True,
                                 env={**os.environ, **(env or {}), "JOB_RC": rcf})
        json.dump({"pid": p.pid, "cmd": [str(c) for c in cmd], "cwd": cwd, "log": log, "started": time.time()},
                  open(rec, "w"))
    j = json.load(open(rec))
    budget = max(0.0, JOB_WAIT - (time.time() - _CALL_START))   # whatever is left of this call's 90 s
    t_end = time.time() + (budget if wait is None else min(wait, budget))
    while True:
        if os.path.exists(rcf):
            return int(open(rcf).read().strip() or -1)
        try:
            os.kill(j["pid"], 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if not alive:
            time.sleep(1)
            if os.path.exists(rcf):
                continue
            raise RuntimeError(f"job {name} (pid {j['pid']}) ended without an exit code; see {log}")
        if time.time() >= t_end:
            mins = (time.time() - j["started"]) / 60
            raise Running(f"job running: {name}, {mins:.0f} min so far (log {log})")
        time.sleep(3)


def run_main(main):
    """Entry point for every adapter: a still-running job becomes not_started, so the step is simply retried."""
    try:
        main()
    except Running as e:
        respond(_REQ, "not_started", error=str(e))
