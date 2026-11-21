#!/usr/bin/env python3
"""Lead nodes: the lead gate (waits for an approval file) and the register update (proposed rows only)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os
import time

from common import deps, load_request, respond, round_config, OUT


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        from common import validate
        return validate(req, True)

    cfg = round_config()
    if tid == "lead_gate":
        path = os.path.join(OUT, "approval.json")
        if not os.path.exists(path):
            return respond(req, "not_started", error="awaiting lead approval (write approval.json, then retry)")
        decision = json.load(open(path))
        qa = deps(req).get("qa_accept", {})
        return respond(req, output={"approved_by": decision.get("by", ""), "decision": qa.get("decision", ""),
                                    "class_rows": qa.get("class_rows", "")})

    if tid == "register_update":
        g = deps(req).get("lead_gate", {})
        row = {"round": cfg["round"], "decision": g.get("decision"), "approved_by": g.get("approved_by"),
               "class_rows": json.loads(g.get("class_rows") or "[]"), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        # Pilot: proposed rows only. Supabase and the Sheets register are wired in step 4.
        with open(os.path.join(OUT, "register_rows_proposed.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
        return respond(req, output={"row": json.dumps(row)[:3000]})

    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    main()
