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

    if cfg.get("kind") == "page_rule_explore":
        return write_plan(out, cfg, explore_tasks())

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
    write_plan(out, cfg, tasks)


def explore_tasks():
    """An exploration round: may get worse within its declared budget; never ships while in debt."""
    return [
        node("pin_inputs", "explore", ["terrain_manifest_sha", "terrain_pinned"], validated=True),
        node("explore_gate", "explore", ["gate_pass", "new_places", "gone_places", "new_within_budget_marginal",
                                         "new_debt", "new_unbudgeted", "debt_items", "target_fixes_held",
                                         "totals_deltas", "totals_over_budget", "expired",
                                         "drop_metres_fixed", "drop_metres_new"], after=["pin_inputs"]),
        node("explore_accept", "explore", ["gate_pass", "decision", "reasons", "class_rows", "pay_back_plan",
                                           "next_steps"], after=["explore_gate"], validated=True),
        node("lead_gate", "lead", ["approved_by", "decision", "class_rows"], after=["explore_accept"]),
        node("register_update", "lead", ["row"], after=["lead_gate"]),
    ]


def write_plan(out, cfg, tasks):
    providers = {p: {"command": [PY, "-I", os.path.join(HERE, "adapters", p + ".py")], "env": ENV}
                 for p in ("terrain", "qa", "lead", "explore")}
    plan = {"api_version": "swarm/v1", "name": "round: " + cfg["round"], "max_tasks": len(tasks) + 2,
            "providers": providers, "tasks": tasks}
    json.dump(plan, open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(os.path.join(out, "plan.json"))


if __name__ == "__main__":
    main()
