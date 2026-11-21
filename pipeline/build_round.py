#!/usr/bin/env python3
"""Turn a round file (rounds/*.json) into one Irrigator graph and a run folder.

usage: build_round.py rounds/r6r_shadow.json OUT_DIR
Writes OUT_DIR/round.json (read by every adapter) and OUT_DIR/plan.json (the graph).
Then run, with NYC_ROOT and PIPELINE_OUT=OUT_DIR in the environment:
    irrigator apply OUT_DIR/plan.json --run OUT_DIR/run --backend mock --workers 3
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = "/usr/bin/python3"
ENV = ["PATH", "HOME", "USER", "NYC_ROOT", "PIPELINE_OUT"]  # USER: the Claude CLI needs it to find its sign-in


def node(i, provider, requires, after=(), fields=None, validated=False, prompt=None):
    t = {"id": i, "provider": provider, "prompt": prompt or i, "requires": requires}
    if after:
        t["depends_on"] = list(after)
    if fields:
        t["input_fields"] = fields
    if validated:
        t["checks"] = [{"kind": "command", "command": [PY, "-I", os.path.join(HERE, "adapters", provider + ".py")],
                        "timeout_seconds": 120, "env": ENV}]
    return t


def main():
    cfg_path, out = sys.argv[1], os.path.abspath(sys.argv[2])
    cfg = json.load(open(cfg_path))
    os.makedirs(out, exist_ok=False)  # a new folder per run, never reused
    json.dump(cfg, open(os.path.join(out, "round.json"), "w"), indent=1)

    pairs = list(cfg["frame_pairs"])
    hyd = [f"hydrogen_{p}" for p in pairs]
    tasks = [
        # Lead: the plan is approved by the act of building the round.
        node("pin_inputs", "terrain", ["terrain_manifest_sha", "highway_sums_sha", "terrain_pinned", "highway_pinned"],
             validated=True),
        node("terrain_checks", "terrain", ["crest_summary", "free_ends", "report_excerpt"], after=["pin_inputs"]),
        node("terrain_accept", "terrain", ["decision", "reasons"], after=["pin_inputs", "terrain_checks"],
             validated=True),
        node("disk_guard", "qa", ["free_gb"], after=["terrain_accept"], validated=True),
        node("drive_gate", "qa", ["a_visible", "a_same_as_live", "a_new", "b_deck_gap_m", "b_deck_lines_with_gap",
                                  "c_new_vs_prev_round_m", "c_new_vs_prev_round_places",
                                  "c_ii_regression_vs_live_m", "c_ii_regression_vs_live_places",
                                  "c_i_newly_detected_m"], after=["disk_guard"]),
        node("city_census", "qa", ["citywide_excerpt"], after=["disk_guard"]),
    ] + [node(h, "qa", ["verdict", "fixed", "new_in_after", "seconds"], after=["disk_guard"], validated=True)
         for h in hyd] + [
        # The drive gate only measures; QA accept computes the gate verdict in code (gate_pass) and its
        # validator needs BOTH gate_pass and Claude's accept, so the model can never override a failed gate.
        node("qa_accept", "qa", ["gate_pass", "decision", "reasons", "class_rows", "next_steps"],
             after=["terrain_checks", "drive_gate", "city_census"] + hyd, validated=True),
        node("lead_gate", "lead", ["approved_by", "decision", "class_rows"], after=["qa_accept"]),
        node("register_update", "lead", ["row"], after=["lead_gate"]),
    ]
    providers = {p: {"command": [PY, "-I", os.path.join(HERE, "adapters", p + ".py")], "env": ENV}
                 for p in ("terrain", "qa", "lead")}
    plan = {"api_version": "swarm/v1", "name": "terrain round: " + cfg["round"], "max_tasks": len(tasks) + 2,
            "providers": providers, "tasks": tasks}
    json.dump(plan, open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(os.path.join(out, "plan.json"))


if __name__ == "__main__":
    main()
