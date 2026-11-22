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
    if cfg.get("kind") in ("page_round", "terrain_round_live", "highway_round") and not cfg.get("code_roots"):
        # the lead (2026-11-22): the round's own adapters / providers are pinned too, not only the project's scripts;
        # written into this run's round.json at build, so runs built earlier keep their own roots
        sys.path.insert(0, os.path.join(HERE, "adapters"))
        from codepin import ROOTS
        cfg["code_roots"] = ROOTS[cfg["kind"]] + [os.path.join(HERE, "adapters")]
    json.dump(cfg, open(os.path.join(out, "round.json"), "w"), indent=1)

    if cfg.get("kind") == "page_rule_explore":
        return write_plan(out, cfg, explore_tasks())
    if cfg.get("kind") == "highway_round":
        return write_plan(out, cfg, highway_tasks(cfg))
    if cfg.get("kind") == "page_round":
        return write_plan(out, cfg, page_tasks(cfg))
    if cfg.get("kind") == "research_swarm":
        return write_plan(out, cfg, swarm_tasks(cfg))
    if cfg.get("kind") == "terrain_round_live":
        return write_plan(out, cfg, terrain_live_tasks(list(cfg.get("frame_pairs") or {}), cfg))

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


def lane_guard_tasks(cfg):
    """Register row 27 (the lead): terrain above a highway lane. adapters/laneguard.py; stops feed qa_accept."""
    if not cfg or not cfg.get("lane_guard"):
        return [], []
    t = [node("lg_manifest", "laneguard", ["pass", "detail"], after=["t_freeze"], validated=True),
         node("lg_over_this", "laneguard", ["out", "triangles", "by_depth"], after=["lg_manifest"]),
         node("lg_over_prev", "laneguard", ["out", "triangles", "by_depth"], after=["lg_manifest"]),
         node("lg_edges", "laneguard", ["out", "new_edge_gaps"], after=["lg_manifest"]),
         node("lg_gate_over", "laneguard", ["pass", "detail", "items"], after=["lg_over_this", "lg_over_prev"], validated=True),
         node("lg_gate_edges", "laneguard", ["pass", "detail", "items"], after=["lg_edges"], validated=True)]
    return t, ["lg_manifest", "lg_gate_over", "lg_gate_edges"]


def terrain_live_tasks(pairs, cfg=None):
    """Live terrain round: terrain study build -> checks -> accept -> freeze, then drive-test QA where every gate
    is its own mandatory step; the QA accept and lead gate can't be reached while any gate is failed or waiting."""
    hyd = [f"hydrogen_{p}" for p in pairs]
    gates = [f"gate_{g}" for g in GATES]
    lgt, lgs = lane_guard_tasks(cfg)
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
         for h in hyd] + lgt + [
        # BUNDLE (shippable vs live) is reported on every round and never blocks the round itself.
        node("bundle", "round_live", ["shippable", "blocking", "line"], after=["qa_round_gate"]),
        node("qa_accept", "round_live", ["rules_pass", "decision", "reasons", "bundle"],
             after=gates + hyd + ["frames_ready", "bundle"] + lgs, validated=True),
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
    if cfg.get("lane_void_check"):   # r14w: driver-height void guard (lead 21 Nov)
        stops.append("d_lane_void")
    if cfg.get("tile_diff"):   # r14t2 (the lead): every file differing from a frozen round, each with a reason
        stops.append("d_tile_diff")
    t = [node("h_guard", "highway", ["pass", "writes", "free_gb"], validated=True),
         node("h_assemble", "highway", ["pass"], after=["h_guard"], validated=True)] + passes + [
         node("h_part_move", "highway", ["pass"], after=[last], validated=True),
         ] + after_pm + [
         node("h_cbe_copy", "highway", ["pass"], after=[after_pm[-1]["id"] if after_pm else "h_part_move"],
              validated=True)] + [
         node(s, "highway", ["pass", "detail"], after=["h_cbe_copy"], validated=True) for s in stops] + [
         node("d_listed", "highway", ["pass", "detail"], after=["h_cbe_copy"]),
         ] + ([node("frames_ready", "highway", ["images", "request"], after=stops + ["d_listed"]),
               node("hydrogen_frames", "highway", ["frames", "verdicts", "rows", "capture_guard"], after=["frames_ready"],
                    validated=True)] if cfg.get("frames") else []) + [
         node("highway_accept", "round_live", ["rules_pass", "decision", "reasons"],
              after=stops + ["d_listed"] + (["hydrogen_frames"] if cfg.get("frames") else []), validated=True),
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
            "modelled_census": ["out", "inherited_lines", "inherited_m", "self_lines", "self_m", "free_lines", "free_m",
                                "inherited_by_r2", "inherited_street_lines", "inherited_street_m", "inherited_other_lines",
                                "inherited_other_m", "inherited_by_class", "freed_rows"],
            "lift_guards": ["out", "tiles", "vacate_polys", "step_cells", "defect_step_cells"],
            "census_fp": ["out", "tents_n", "tents_m2", "overflow_polys"],
            "fp_new": ["out", "rows"], "kept_lidar2": ["out", "kept", "counts"],
            "kept_lidar_dual": ["out", "kept", "counts"],
            "census_rs": ["out", "tents_n", "tents_m2", "overflow_polys", "rs_whole_losses", "rs_keep_test"]}


def page_tasks(cfg):
    """The page editor's tools (its round-graph spec) as steps in the order the round file lists them;
    gates computed in code against the declared budget; frames are a hand-off the page-editor session fills."""
    steps = [s["id"] for s in cfg["steps"]]
    t = [node("p_disk", "page", ["free_gb"])]
    t += [node(s["id"], "page", PAGE_OUT[s["tool"]], after=["p_disk"] + s.get("after", []), prompt=s["tool"])
          for s in cfg["steps"]]
    gates = [g["id"] for g in cfg.get("gates", [])]
    # a gate on step "frames" (G0 capture, G3 structure) reads the frames folder: it runs after frames_ready and
    # Hydrogen waits for it (a stopping check before Hydrogen)
    # a gate on step "hydrogen" reads Hydrogen's rows: it runs after hydrogen_frames
    on = lambda g: (["frames_ready"] if g["step"] == "frames" else ["hydrogen_frames"] if g["step"] == "hydrogen"
                    else [g["step"]])
    t += [node(g["id"], "page", ["pass", "detail", "items"], after=on(g) + ([g["base_step"]] if g.get("base_step") else []) + list(g.get("also", [])),
               validated=True) for g in cfg.get("gates", [])]
    fgates = [g["id"] for g in cfg.get("gates", []) if g["step"] == "frames"]
    fr = []
    if cfg.get("frames"):
        t += [node("frames_ready", "page", ["images", "request"], after=cfg["frames"].get("after", steps)),
              node("hydrogen_frames", "page", ["frames", "verdicts", "rows"], after=["frames_ready"] + fgates,
                   validated=True)]
        fr = ["hydrogen_frames"]
    if cfg.get("gates_only"):   # a companion round: measured steps and gates only, reported with its main round
        return t
    t += [node("page_accept", "page", ["rules_pass", "decision", "reasons", "class_rows", "next_steps"],
               after=steps + gates + fr, validated=True),
          node("lead_gate", "lead", ["approved_by", "decision", "class_rows", "bundle"], after=["page_accept"]),
          node("register_update", "lead", ["row"], after=["lead_gate"])]
    return t


def swarm_tasks(cfg):
    """research_swarm (adapters/swarm.py): A lanes x B items x C helpers. Per item: 'before' helpers (no deps: they
    start at once), the main agent after them and after the previous item's main in its lane (so a lane is sequential
    and carries its last summary), the check gate, and 'after' helpers. swarm_report after every main and helper
    (it reads the checks from the run state, so one failed check doesn't hide the measurements)."""
    H = ["findings", "contradictions", "minutes_wall", "cost_usd", "file", "tokens"]
    F = ["gate_before", "contradictions_in", "ran", "minutes_wall", "cost_usd", "tokens"]
    M = ["card", "summary", "conf_counts", "open_questions", "minutes_wall", "cost_usd", "minutes_reported", "denied_tool_calls",
         "hints_conf", "helpers", "self_check", "tokens"]
    t, last = [], []
    for lane in cfg["lanes"]:
        prev = None
        for it in map(str, lane):
            bef = [f"{it}__{h['id']}" for h in cfg.get("helpers", []) if h.get("when", "before") == "before"]
            aft = [f"{it}__{h['id']}" for h in cfg.get("helpers", []) if h.get("when") == "after"]
            t += [node(b, "swarm", H, prompt=f"helper {b}") for b in bef]
            t.append(node(f"{it}__main", "swarm", M, after=bef + ([f"{prev}__main"] if prev else []), prompt=f"research {it}"))
            t += [node(a, "swarm", H, after=[f"{it}__main"], prompt=f"helper {a}") for a in aft]
            fix = [f"{it}__fix"] if cfg.get("fix") else []   # gate output + contradictions back to the main, once
            t += [node(x, "swarm", F, after=[f"{it}__main"] + aft, prompt=f"fix {it}") for x in fix]
            t.append(node(f"{it}__check", "swarm", ["pass", "detail"], after=fix or [f"{it}__main"], validated=True))
            last += bef + [f"{it}__main", f"{it}__check"] + aft + fix
            prev = it
    t.append(node("swarm_report", "swarm", ["items", "total_cost_usd", "cost_per_item_usd", "main_minutes_median",
                                            "checks_passed", "report"], after=[x for x in last if not x.endswith("__check")]))
    return t


FINAL_ACCEPT = ("page_accept", "qa_accept", "highway_accept", "explore_accept")


def freeze(cfg, tasks):
    """The lead's code-freeze rule: code_pin before every root step, code_verify after every measuring step and
    before the final accept (or last, in a gates-only round)."""
    if cfg.get("kind") not in ("page_round", "terrain_round_live", "highway_round"):
        return tasks
    for t in tasks:
        if not t.get("depends_on"):
            t["depends_on"] = ["code_pin"]
    tail = {"lead_gate", "register_update", *FINAL_ACCEPT}
    measured = [t["id"] for t in tasks if t["id"] not in tail]
    for t in tasks:
        if t["id"] in FINAL_ACCEPT:
            t["depends_on"] = list(t.get("depends_on") or []) + ["code_verify"]
    return ([node("code_pin", "codepin", ["pass", "detail"], validated=True)] + tasks
            + [node("code_verify", "codepin", ["pass", "detail"], after=measured, validated=True)])


def write_plan(out, cfg, tasks):
    tasks = freeze(cfg, tasks)
    providers = {p: {"command": [PY, "-I", os.path.join(HERE, "adapters", p + ".py")], "env": ENV}
                 for p in ("terrain", "qa", "lead", "explore", "round_live", "highway", "page", "codepin", "swarm", "laneguard")}
    plan = {"api_version": "swarm/v1", "name": "round: " + cfg["round"], "max_tasks": len(tasks) + 2,
            "providers": providers, "tasks": tasks}
    json.dump(plan, open(os.path.join(out, "plan.json"), "w"), indent=1)
    print(os.path.join(out, "plan.json"))


if __name__ == "__main__":
    main()
