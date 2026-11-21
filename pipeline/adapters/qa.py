#!/usr/bin/env python3
"""QA coordinator nodes: disk guard, drive gate, city census, Hydrogen reviews, QA accept (Claude)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os
import re
import shutil
import subprocess
import time

from common import (claude_json, deps, load_request, project, respond, round_config, validate, OUT)

MIN_FREE_GB = 5
HYDROGEN = "gemini-4-hydrogen-high"
ASK = ("These are frames from a 3D city driving game, BEFORE and AFTER one change, from the same camera. "
       "List the visible rendering defects (missing road, floating or misplaced geometry, walls or slabs where they "
       "should not be, drops, gaps, z-fighting, see-through holes, misplaced paint) in BEFORE and in AFTER. Say which "
       "BEFORE defects are fixed in AFTER and which are new in AFTER, and where in the frame. If they look the same, "
       "say so. End with exactly one JSON block:\n```json\n{\"fixed\": [\"...\"], \"new_in_after\": [\"...\"], "
       "\"verdict\": \"better|same|worse|mixed\"}\n```")
QA_SCHEMA = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["accept", "reject"]},
    "reasons": {"type": "array", "items": {"type": "string"}},
    "class_rows": {"type": "array", "items": {"type": "object", "properties": {
        "class": {"type": "string"}, "count": {"type": "string"}, "status": {"type": "string"}},
        "required": ["class", "count", "status"]}},
    "next_steps": {"type": "array", "items": {"type": "string"}}},
    "required": ["decision", "reasons", "class_rows", "next_steps"]}


def num(s):
    return int(s.replace(",", ""))


def drive_gate(cfg):
    rep = open(project(cfg["exact_report"])).read()
    a = re.search(r"r6r \+ r14s, B′\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)", rep)
    prev = re.search(r"against r6q: ([\d,]+) m new at (\d+) places", rep)
    split = json.load(open(project(cfg["gate_c_split"])))
    gap = json.load(open(project(cfg["deck_gap_census"])))["total"]
    return {
        "a_visible": a.group(1), "a_same_as_live": a.group(2), "a_new": a.group(3),
        "b_deck_gap_m": round(gap["underM"]), "b_deck_lines_with_gap": gap["linesUnder"],
        "c_new_vs_prev_round_m": num(prev.group(1)) if prev else -1,
        "c_new_vs_prev_round_places": prev.group(2) if prev else "?",
        "c_ii_regression_vs_live_m": round(split["ii"]["m"]), "c_ii_regression_vs_live_places": len(split["ii"]["places"]),
        "c_i_newly_detected_m": round(split["i"]["m"]),
    }


def hydrogen(req, cfg, pair_id):
    before, after = cfg["frame_pairs"][pair_id]
    d = os.path.join(OUT, "hydrogen", f"{pair_id}_a{req.get('attempt', 1)}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    names = []
    for label, src in (("before", before), ("after", after)):
        if src is None:
            continue
        n = label + os.path.splitext(src)[1]
        shutil.copy2(project(src), os.path.join(d, n))
        names.append(n)
    where = (f"BEFORE is {names[0]}, AFTER is {names[1]}." if len(names) == 2 else
             f"{names[0]} is one side-by-side image: the LEFT half is BEFORE, the RIGHT half is AFTER.")
    pre = (f"The images are in the folder {d} : {' '.join(names)}. {where} Open and look at every one of them with "
           "your file/image viewing tool. Do not run shell commands and do not open any other files.")
    t0 = time.time()
    try:
        p = subprocess.run(["agy", "-p", pre + "\n\n" + ASK, "--model", HYDROGEN, "--add-dir", d,
                            "--settings", '{"permissions":{"deny":["command(*)"]}}', "--print-timeout", "600s",
                            "--output-format", "json"], cwd=d, capture_output=True, text=True, timeout=660)
    except subprocess.TimeoutExpired:
        return respond(req, "uncertain", error="agy timed out")
    open(os.path.join(d, "agy_stdout.json"), "w").write(p.stdout)
    try:
        j = json.loads(p.stdout)
    except ValueError:
        return respond(req, "uncertain", error=f"agy rc={p.returncode}, unparseable output")
    text = j.get("response", "")
    m = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    v = json.loads(m[-1]) if m else {}
    respond(req, output={"verdict": v.get("verdict", ""), "fixed": json.dumps(v.get("fixed", [])),
                         "new_in_after": json.dumps(v.get("new_in_after", [])),
                         "seconds": round(time.time() - t0, 1)})


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        if tid == "disk_guard":
            return validate(req, float(c.get("free_gb", 0)) >= MIN_FREE_GB)
        if tid == "qa_accept":
            return validate(req, c.get("gate_pass") == "true" and c.get("decision") == "accept")
        if tid.startswith("hydrogen_"):
            return validate(req, c.get("verdict") in ("better", "same", "worse", "mixed"))
        return validate(req, True)

    cfg = round_config()
    if tid == "disk_guard":
        free = shutil.disk_usage(project(".")).free / 1e9
        return respond(req, output={"free_gb": round(free, 1)})
    if tid == "drive_gate":
        return respond(req, output=drive_gate(cfg))
    if tid == "city_census":
        rep = open(project(cfg["exact_report"])).read()
        i = rep.find("Citywide")
        return respond(req, output={"citywide_excerpt": rep[i:i + 1500] if i >= 0 else "not found"})
    if tid.startswith("hydrogen_"):
        return hydrogen(req, cfg, tid[len("hydrogen_"):])
    if tid == "qa_accept":
        g = deps(req).get("drive_gate", {})
        # Lead rule (21 Nov): no new free end, no route losing contact where live draws a higher road (class ii),
        # nothing new against the previous round. Deck-gap metres are reported; no threshold yet.
        gate_pass = (g.get("a_new") == "0" and g.get("c_ii_regression_vs_live_places") == "0"
                     and g.get("c_new_vs_prev_round_m") == "0")
        prompt = (open(os.path.join(os.path.dirname(__file__), "..", "prompts", "qa_accept.md")).read()
                  + "\n\nRound: " + cfg["round"]
                  + "\n\nDrive gate verdict, computed in code from the rule above: " + ("PASS" if gate_pass else "FAIL")
                  + "\n\nEvidence (outputs of the nodes this one depends on):\n"
                  + json.dumps(deps(req), indent=1)[:16000])
        try:
            v = claude_json(prompt, QA_SCHEMA, cwd=OUT)
        except Exception as e:
            return respond(req, "uncertain", error=f"claude failed: {e}"[:300])
        with open(os.path.join(OUT, f"qa_accept_a{req.get('attempt', 1)}.json"), "w") as f:
            json.dump(v, f, indent=1)  # kept even when the validator then fails the node on a reject
        return respond(req, output={"gate_pass": str(gate_pass).lower(), "decision": v["decision"],
                                    "reasons": json.dumps(v["reasons"]),
                                    "class_rows": json.dumps(v["class_rows"]),
                                    "next_steps": json.dumps(v["next_steps"])})
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    main()
