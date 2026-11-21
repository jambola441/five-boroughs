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
ENV = ["PATH", "HOME", "USER", "NYC_ROOT", "PIPELINE_OUT", "PE_ROOT"]  # USER: the Claude CLI needs it to find its sign-in


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
    if cfg.get("kind") == "highway_round":
        return write_plan(out, cfg, highway_tasks(cfg))
    if cfg.get("kind") == "page_round":
        return write_plan(out, cfg, page_tasks(cfg))
    if cfg.get("kind") == "terrain_round_live":
        return write_plan(out, cfg, terrain_live_tasks(list(cfg.get("frame_pairs") or {})))

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
        node("lead_gate", "lead", ["approved_by", "decision", "class_rows", "bundle"], after=["qa_accept"]),
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


GATES = ["pins", "a", "b", "c", "ramps", "city", "walls", "flight"]  # the lead's mandatory gates (21 Nov)


def terrain_live_tasks(pairs):
    """Live terrain round: terrain study build -> checks -> accept -> freeze, then drive-test QA where every gate
    is its own mandatory step; the QA accept and lead gate can't be reached while any gate is failed or waiting."""
    hyd = [f"hydrogen_{p}" for p in pairs]
    gates = [f"gate_{g}" for g in GATES]
    t = [
        node("t_build", "round_live", ["built", "frozen"], validated=True),
        node("t_checks", "round_live", ["free_ends", "crest_8pct", "crest_12pct", "crest_widened_8",
                                        "crest_widened_12", "join_steps_gt_0p1",
                                        "junctions_over_2", "junctions_max", "differ", "pass", "fails"],
             after=["t_build"], validated=True),
        node("terrain_accept", "round_live", ["rules_pass", "decision", "reasons"], after=["t_checks"], validated=True),
        node("t_freeze", "round_live", ["terrain_sha", "verified", "matches_expected"], after=["terrain_accept"],
             validated=True),
        node("qa_disk", "round_live", ["free_gb"], after=["t_freeze"]),
        node("qa_round_gate", "round_live", ["gate_log", "gate_dir", "ran"], after=["qa_disk", "t_freeze"]),
    ] + [node(g, "round_live", ["pass", "detail"], after=["qa_round_gate"], validated=True) for g in gates] + [
        node("frames_ready", "round_live", ["pairs"], after=["t_freeze"]),
    ] + [node(h, "qa", ["verdict", "fixed", "new_in_after", "seconds"], after=["frames_ready"], validated=True)
         for h in hyd] + [
        # BUNDLE (shippable vs live) is reported on every round and never blocks the round itself.
        node("bundle", "round_live", ["shippable", "blocking", "line"], after=["qa_round_gate"]),
        node("qa_accept", "round_live", ["rules_pass", "decision", "reasons", "bundle"],
             after=gates + hyd + ["frames_ready", "bundle"], validated=True),
        node("lead_gate", "lead", ["approved_by", "decision", "class_rows", "bundle"], after=["qa_accept"]),
        node("register_update", "lead", ["row"], after=["lead_gate"]),
    ]
    return t


def highway_tasks(cfg):
    """macmini's make chain: A -> B passes in order -> C, then D checks after C (void chain inside one step).
    Stop checks are mandatory steps; listed checks are reported. crossing/covered checks join when written."""
    passes = [node(f"h_pass_{i:02d}", "highway", ["pass", "step"], after=[f"h_pass_{i - 1:02d}" if i else "h_assemble"],
                   validated=True, prompt=f"post-pass {s}") for i, s in enumerate(cfg["passes"])]
    last = f"h_pass_{len(cfg['passes']) - 1:02d}"
    after_pm = [node(f"h_after_{i:02d}", "highway", ["pass", "step"],
                     after=[f"h_after_{i - 1:02d}" if i else "h_part_move"], validated=True, prompt=f"after part_move: {s}")
                for i, s in enumerate(cfg.get("after_part_move", []))]
    stops = ["d_floor_fix_guard", "d_tile_inventory", "d_no_surface", "d_unsupported", "d_underlay", "d_void", "d_walls", "d_columns"]
    if cfg.get("crossing_check"):   # macmini switches this on once its r14u rules are in
        stops.append("d_crossing")
    t = [node("h_guard", "highway", ["pass", "writes", "free_gb"], validated=True),
         node("h_assemble", "highway", ["pass"], after=["h_guard"], validated=True)] + passes + [
         node("h_part_move", "highway", ["pass"], after=[last], validated=True),
         ] + after_pm + [
         node("h_cbe_copy", "highway", ["pass"], after=[after_pm[-1]["id"] if after_pm else "h_part_move"],
              validated=True)] + [
         node(s, "highway", ["pass", "detail"], after=["h_cbe_copy"], validated=True) for s in stops] + [
         node("d_listed", "highway", ["pass", "detail"], after=["h_cbe_copy"]),
         node("highway_accept", "round_live", ["rules_pass", "decision", "reasons"], after=stops + ["d_listed"],
              validated=True),
         node("lead_gate", "lead", ["approved_by", "decision", "class_rows", "bundle"], after=["highway_accept"]),
         node("register_update", "lead", ["row"], after=["lead_gate"])]
    return t


PAGE_OUT = {"drape": ["out", "tiles_written", "tiles_total"],
            "lift_gate": ["out", "flags", "visible_drivable_new", "lost_contact_flags", "lost_contact_new_places",
                          "car_steps_2m", "car_ledges_2m", "curbs_km", "c_ii_m", "gate_lines"],
            "newlist": ["out", "new_places", "new_step2", "new_lost", "gone_places", "gone_step2", "gone_lost"],
            "deck_under": ["out", "underM", "noneM", "linesUnder", "zero_piece_underM"],
            "ramps": ["out", "flattened", "previous", "new", "gone", "rampgate", "line"],
            "trace": ["out", "sites", "counts"], "lidar": ["out", "sites", "counts", "classes"],
            "census": ["out", "tents_n", "tents_m2", "overflow_polys"],
            "kept_lidar": ["out", "kept", "counts"],
            "tent_diff": ["out", "n_new", "m2_new", "tiles_changed"],
            "census_rs": ["out", "tents_n", "tents_m2", "overflow_polys", "rs_whole_losses", "rs_keep_test"]}


def page_tasks(cfg):
    """The page editor's tools (its round-graph spec) as steps in the order the round file lists them;
    gates computed in code against the declared budget; frames are a hand-off the page-editor session fills."""
    steps = [s["id"] for s in cfg["steps"]]
    t = [node("p_disk", "page", ["free_gb"])]
    t += [node(s["id"], "page", PAGE_OUT[s["tool"]], after=["p_disk"] + s.get("after", []), prompt=s["tool"])
          for s in cfg["steps"]]
    gates = [g["id"] for g in cfg.get("gates", [])]
    t += [node(g["id"], "page", ["pass", "detail", "items"], after=[g["step"]] + ([g["base_step"]] if g.get("base_step") else []),
               validated=True) for g in cfg.get("gates", [])]
    fr = []
    if cfg.get("frames"):
        t += [node("frames_ready", "page", ["images", "request"], after=cfg["frames"].get("after", steps)),
              node("hydrogen_frames", "page", ["frames", "verdicts", "rows"], after=["frames_ready"], validated=True)]
        fr = ["hydrogen_frames"]
    if cfg.get("gates_only"):   # a companion round: measured steps and gates only, reported with its main round
        return t
    t += [node("page_accept", "page", ["rules_pass", "decision", "reasons", "class_rows", "next_steps"],
               after=steps + gates + fr, validated=True),
          node("lead_gate", "lead", ["approved_by", "decision", "class_rows", "bundle"], after=["page_accept"]),
          node("register_update", "lead", ["row"], after=["lead_gate"])]
    return t


def write_plan(out, cfg, tasks):
    providers = {p: {"command": [PY, "-I", os.path.join(HERE, "adapters", p + ".py")], "env": ENV}
                 for p in ("terrain", "qa", "lead", "explore", "round_live", "highway", "page")}
    plan = {"api_version": "swarm/v1", "name": "round: " + cfg["round"], "max_tasks": len(tasks) + 2,
            "providers": providers, "tasks": tasks}
    json.dump(plan, open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(os.path.join(out, "plan.json"))


if __name__ == "__main__":
    main()
