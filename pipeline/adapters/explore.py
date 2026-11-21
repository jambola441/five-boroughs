#!/usr/bin/env python3
"""Exploration-round nodes: a round that may get worse on purpose, within a declared budget.

explore_gate measures the candidate against the baseline and the round's own budget, debts, target and expiry.
explore_accept (Claude) decides whether to keep exploring; its validator also needs the gate verdict,
computed in code, so the model cannot wave through an over-budget round.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import datetime
import json
import math
import os

from common import (coordinator_json, deps, load_request, project, respond, round_config, sha256, validate, OUT)

SCHEMA = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["keep_exploring", "stop"]},
    "reasons": {"type": "array", "items": {"type": "string"}},
    "class_rows": {"type": "array", "items": {"type": "object", "properties": {
        "class": {"type": "string"}, "count": {"type": "string"}, "status": {"type": "string"}},
        "required": ["class", "count", "status"]}},
    "pay_back_plan": {"type": "array", "items": {"type": "string"}},
    "next_steps": {"type": "array", "items": {"type": "string"}}},
    "required": ["decision", "reasons", "class_rows", "pay_back_plan", "next_steps"]}


def near(item, pts, r):
    return any(math.hypot(item["x"] - p["x"], item["z"] - p["z"]) <= p.get("radius_m", r) for p in pts)


def gate(cfg):
    d = json.load(open(project(cfg["new_gone_list"])))
    b = cfg["budget"]
    new, gone = d["newList"], d["goneList"]
    debt = [i for i in new if near(i, cfg["debt"], 30)]
    marginal = [i for i in new if i not in debt and i["c"] == "step2" and i["mag"] < b["marginal_step_max_m"]]
    unbudgeted = [i for i in new if i not in debt and i not in marginal]
    fixed_ok = [t["name"] for t in cfg["target_fixed"] if near(t, [dict(g, radius_m=15) for g in gone], 15)]
    deltas, over = {}, []
    for k, (base, cand) in cfg["totals"].items():
        dv = round(cand - base, 2)
        deltas[k] = dv
        if dv > b.get(k + "_delta", 0):
            over.append(f"{k} {base} -> {cand} (+{dv}, budget +{b.get(k + '_delta', 0)})")
    expired = datetime.date.today() > datetime.date.fromisoformat(cfg["expires"])
    passed = (not unbudgeted and len(marginal) <= b["marginal_steps_new_places"] and not over
              and len(fixed_ok) == len(cfg["target_fixed"]) and not expired)
    return {
        "gate_pass": str(passed).lower(),
        "new_places": len(new), "gone_places": len(gone),
        "new_within_budget_marginal": len(marginal), "new_debt": len(debt),
        "new_unbudgeted": json.dumps([f"{i['rname']} {i['c']} {i['mag']} m at {i['x']:.0f},{i['z']:.0f}" for i in unbudgeted]),
        "debt_items": json.dumps([f"{i['rname']} {i['c']} {i['mag']} m" for i in debt]),
        "target_fixes_held": f"{len(fixed_ok)} of {len(cfg['target_fixed'])}",
        "totals_deltas": json.dumps(deltas), "totals_over_budget": json.dumps(over),
        "expired": str(expired).lower(),
        "drop_metres_fixed": round(sum(i["mag"] for i in gone if i["c"] == "step2"), 2),
        "drop_metres_new": round(sum(i["mag"] for i in new if i["c"] == "step2"), 2),
    }


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        if tid == "pin_inputs":
            return validate(req, c.get("terrain_pinned") == "true")
        if tid == "explore_accept":
            return validate(req, c.get("gate_pass") == "true" and c.get("decision") == "keep_exploring")
        return validate(req, True)

    cfg = round_config()
    if tid == "pin_inputs":
        t = sha256(project(cfg["terrain_manifest"]))
        return respond(req, output={"terrain_manifest_sha": t, "terrain_pinned": str(t == cfg["terrain_manifest_sha"]).lower()})
    if tid == "explore_gate":
        return respond(req, output=gate(cfg))
    if tid == "explore_accept":
        g = deps(req).get("explore_gate", {})
        brief = {k: cfg[k] for k in ("idea", "baseline_rule", "candidate_rule", "budget", "debt", "target_fixed",
                                     "expires", "max_rounds", "totals", "totals_source", "known_gaps")}
        prompt = (open(os.path.join(os.path.dirname(__file__), "..", "prompts", "explore_accept.md")).read()
                  + "\n\nRound: " + cfg["round"] + "\n\nThe round as declared up front:\n" + json.dumps(brief, indent=1)
                  + "\n\nExploration gate verdict, computed in code: " + ("PASS" if g.get("gate_pass") == "true" else "FAIL")
                  + "\n\nEvidence:\n" + json.dumps(deps(req), indent=1)[:14000])
        try:
            v = coordinator_json(prompt, SCHEMA, cwd=OUT, spec=cfg.get("coordinator"))
        except Exception as e:
            return respond(req, "uncertain", error=f"coordinator failed: {e}"[:300])
        with open(os.path.join(OUT, f"explore_accept_a{req.get('attempt', 1)}.json"), "w") as f:
            json.dump(v, f, indent=1)
        return respond(req, output={"gate_pass": g.get("gate_pass", "false"), "decision": v["decision"],
                                    "reasons": json.dumps(v["reasons"]), "class_rows": json.dumps(v["class_rows"]),
                                    "pay_back_plan": json.dumps(v["pay_back_plan"]),
                                    "next_steps": json.dumps(v["next_steps"])})
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    main()
