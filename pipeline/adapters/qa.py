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

from common import (coordinator_json, deps, load_request, project, respond, round_config, run_job, run_main,
                    validate, OUT)

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


def hydrogen_review(name, images, where, ask=ASK, wait=None):
    """One Hydrogen look at a few frames, as a detached job (agy can take minutes; steps end at 120 s).
    images: [(name_in_folder, source_path)]. The folder holds copies of the frames only; agy may read nothing else.
    Returns the parsed verdict dict plus seconds; raises Running while agy works."""
    d = os.path.join(OUT, "hydrogen", name)
    if not os.path.exists(os.path.join(OUT, "jobs", "hydrogen_" + name, "job.json")):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        for n, src in images:
            shutil.copy2(src, os.path.join(d, n))
    pre = (f"The images are in the folder {d} : {' '.join(n for n, _ in images)}. {where} Open and look at every one "
           "of them with your file/image viewing tool. Do not run shell commands and do not open any other files.")
    rc = run_job("hydrogen_" + name, ["agy", "-p", pre + "\n\n" + ask, "--model", HYDROGEN, "--add-dir", d,
                                      "--settings", '{"permissions":{"deny":["command(*)"]}}', "--print-timeout",
                                      "600s", "--output-format", "json"], d, f"hydrogen/{name}/agy_stdout.json",
                 **({} if wait is None else {"wait": wait}))
    rec = json.load(open(os.path.join(OUT, "jobs", "hydrogen_" + name, "job.json")))
    try:
        j = json.load(open(os.path.join(d, "agy_stdout.json")))
    except ValueError:
        raise RuntimeError(f"agy rc={rc}, unparseable output")
    m = re.findall(r"```json\s*(\{.*?\})\s*```", j.get("response", ""), re.S)
    v = json.loads(m[-1]) if m else {}
    v["seconds"] = round(os.path.getmtime(os.path.join(OUT, "jobs", "hydrogen_" + name, "rc")) - rec["started"], 1)
    return v


def hydrogen(req, cfg, pair_id):
    before, after = cfg["frame_pairs"][pair_id]
    imgs = [(label + os.path.splitext(src)[1], project(src)) for label, src in (("before", before), ("after", after)) if src]
    where = (f"BEFORE is {imgs[0][0]}, AFTER is {imgs[1][0]}." if len(imgs) == 2 else
             f"{imgs[0][0]} is one side-by-side image: the LEFT half is BEFORE, the RIGHT half is AFTER.")
    try:
        v = hydrogen_review(pair_id, imgs, where)
    except RuntimeError as e:
        return respond(req, "uncertain", error=str(e)[:300])
    respond(req, output={"verdict": v.get("verdict", ""), "fixed": json.dumps(v.get("fixed", [])),
                         "new_in_after": json.dumps(v.get("new_in_after", [])), "seconds": v["seconds"]})


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
            v = coordinator_json(prompt, QA_SCHEMA, cwd=OUT, spec=cfg.get("coordinator"))
        except Exception as e:
            return respond(req, "uncertain", error=f"coordinator failed: {e}"[:300])
        with open(os.path.join(OUT, f"qa_accept_a{req.get('attempt', 1)}.json"), "w") as f:
            json.dump(v, f, indent=1)  # kept even when the validator then fails the node on a reject
        return respond(req, output={"gate_pass": str(gate_pass).lower(), "decision": v["decision"],
                                    "reasons": json.dumps(v["reasons"]),
                                    "class_rows": json.dumps(v["class_rows"]),
                                    "next_steps": json.dumps(v["next_steps"])})
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    run_main(main)
