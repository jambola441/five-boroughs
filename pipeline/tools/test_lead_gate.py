#!/usr/bin/env python3
"""r14x4-1 PATCH_1 (the lead, 2026-11-27): lead_gate takes its decision from highway_accept (and terrain_accept) too.
Through a real Irrigator mini-plan: a stub highway_accept answers "accept", then lead.py's lead_gate (with the lead's
approval.json) and register_update run. The patched lead.py must give decision "accept"; the old one (passed as an
argument) must fail with an empty decision, as r14x4-1 did.
run: python3 -I pipeline/tools/test_lead_gate.py [ADAPTERS_DIR]   (default: this repo's adapters)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import build_round as BR   # noqa: E402

ADAPTERS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "adapters")
STUB = r'''import json, sys
req = json.load(sys.stdin)
if "valid" in (req.get("kind") or ""):
    json.dump({"protocol": "swarm.worker/v1", "request_id": req["request_id"], "passed": True, "cost_usd": 0}, sys.stdout)
else:
    json.dump({"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled", "cost_usd": 0,
               "output": {"rules_pass": "true", "decision": "accept", "reasons": json.dumps(["d_walls PASS", "frames PASS"])}},
              sys.stdout)
'''


def main():
    tmp = tempfile.mkdtemp(prefix="leadgate_")
    try:
        run = os.path.join(tmp, "run1")
        os.makedirs(run)
        json.dump({"round": "rlg", "kind": "highway_round"}, open(os.path.join(run, "round.json"), "w"))
        json.dump({"decision": "approve", "by": "lead", "note": "test"}, open(os.path.join(run, "approval.json"), "w"))
        stub = os.path.join(tmp, "stub.py")
        open(stub, "w").write(STUB)
        tasks = [{"id": "highway_accept", "provider": "stub", "prompt": "x", "requires": ["rules_pass", "decision", "reasons"]},
                 BR.node("lead_gate", "lead", ["approved_by", "decision", "class_rows", "bundle"], after=["highway_accept"]),
                 BR.node("register_update", "lead", ["row"], after=["lead_gate"])]
        prov = {"stub": {"command": [sys.executable, "-I", stub], "env": BR.ENV},
                "lead": {"command": [BR.PY, "-I", os.path.join(ADAPTERS, "lead.py")], "env": BR.ENV}}
        json.dump({"api_version": "swarm/v1", "name": "lead gate test", "max_tasks": 5, "providers": prov, "tasks": tasks},
                  open(os.path.join(run, "plan.json"), "w"))
        subprocess.run(["irrigator", "apply", os.path.join(run, "plan.json"), "--run", os.path.join(run, "run"),
                        "--backend", "mock"], env={**os.environ, "PIPELINE_OUT": run, "NYC_ROOT": tmp, "PE_ROOT": ""},
                       capture_output=True, text=True, timeout=300)
        t = json.load(open(os.path.join(run, "run", "state.json")))["tasks"]
        lg, ru = t["lead_gate"], t["register_update"]
        ok = lg["status"] == "verified" and (lg.get("result") or {}).get("decision") == "accept" and ru["status"] == "verified"
        print("lead_gate:", lg["status"], (lg.get("result") or {}).get("decision"), (lg.get("error") or "")[:100],
              "| register_update:", ru["status"])
        if ok:
            row = json.loads(open(os.path.join(run, "register_rows_proposed.jsonl")).read().splitlines()[-1])
            assert row["decision"] == "accept" and row["approved_by"] == "lead" and row["lead_note"] == "test", row
            print("highway_accept 'accept' -> lead_gate decision 'accept' -> register row proposed: ok")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
