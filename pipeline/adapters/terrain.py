#!/usr/bin/env python3
"""Terrain coordinator nodes: pin inputs, terrain checks, terrain accept (Claude)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os
import re

from common import (coordinator_json, deps, load_request, project, respond, round_config, sha256, validate, OUT)

ACCEPT_SCHEMA = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["accept", "reject"]},
    "reasons": {"type": "array", "items": {"type": "string"}}},
    "required": ["decision", "reasons"]}


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        cand = req.get("candidate") or {}
        if tid == "pin_inputs":
            return validate(req, cand.get("terrain_pinned") == "true" and cand.get("highway_pinned") == "true")
        if tid == "terrain_accept":
            return validate(req, cand.get("decision") == "accept")
        return validate(req, True)

    cfg = round_config()
    if tid == "pin_inputs":
        t = sha256(project(cfg["terrain_manifest"]))
        h = sha256(project(cfg["highway_sums"]))
        return respond(req, output={"terrain_manifest_sha": t, "highway_sums_sha": h,
                                    "terrain_pinned": str(t == cfg["terrain_manifest_sha"]).lower(),
                                    "highway_pinned": str(h == cfg["highway_sums_sha"]).lower()})

    if tid == "terrain_checks":
        crest = json.load(open(project(cfg["crest_census"])))
        report = open(project(cfg["terrain_report"])).read()
        line = next((l.strip("-* ") for l in report.splitlines() if re.search(r"[Ff]ree ends", l)), "")
        return respond(req, output={
            "crest_summary": json.dumps(crest.get("summary"))[:1500],
            "free_ends": line or "not found in report",
            "report_excerpt": report[:2500]})

    if tid == "terrain_accept":
        d = deps(req)
        prompt = (open(os.path.join(os.path.dirname(__file__), "..", "prompts", "terrain_accept.md")).read()
                  + "\n\nRound: " + cfg["round"] + "\n\nEvidence:\n" + json.dumps(d, indent=1)[:12000])
        try:
            v = coordinator_json(prompt, ACCEPT_SCHEMA, cwd=OUT, spec=cfg.get("coordinator"))
        except Exception as e:  # sign-in, timeout, bad output: never guess an acceptance
            return respond(req, "uncertain", error=f"coordinator failed: {e}"[:300])
        return respond(req, output={"decision": v["decision"], "reasons": json.dumps(v["reasons"])})

    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    main()
