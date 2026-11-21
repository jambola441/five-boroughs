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


def load_request():
    return json.load(sys.stdin)


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
