#!/usr/bin/env python3
"""Highway round (macmini's make chain) as graph steps, wrapping its scripts unchanged.

Source of truth: macmini's make_<round>.sh and ROUND_STEPS_<round>.md. Same commands, env and order:
A assemble (POST_ONLY: the input build is reused, never rebuilt) -> B post-passes in the listed order ->
C CBE copy + hard-link _nocbe copy -> D checks (stop checks are mandatory steps, pass/fail in code).
macmini owns highways/ and edits the pass scripts; this file only calls them.

Write paths (nothing else is written): highways/work/p2net/final_fdr/<round>, .../<round>_nocbe,
.../<round>_profile_*.json, .../<round>_nocbe_profile_all.json, and the scripts' own
highways/work/p2net/<check>_<round>[_nocbe].json outputs. The guard refuses to start if the round folder already
exists, is a frozen round, or the input build would be rebuilt. No step uploads, pushes, downloads or deletes
anything outside the new round's own folders.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os
import shutil
import subprocess

from common import Running, deps, load_request, project, respond, round_config, run_job, run_main, sha256, validate, OUT

HW = "highways"
CBE = "x10_z-17 x10_z-18 x11_z-18 x12_z-18 x13_z-17 x13_z-18 x14_z-17 x15_z-17 x8_z-18 x9_z-17 x9_z-18".split()


def paths(cfg):
    r = cfg["round"]
    return {"R": f"work/p2net/final_fdr/{r}", "RC": f"work/p2net/final_fdr/{r}_nocbe",
            "B": cfg["input_build"], "prev_nocbe": f"work/p2net/final_fdr/{cfg['prev']}_nocbe"}


def run(cfg, argv, log, env=None, cwd=None, wait=None):
    """macmini's own command, unchanged, as a detached job (see common.run_job); output goes to log."""
    py = os.path.join(os.environ["NYC_ROOT"], ".venv/bin/python")
    argv = [py if a == "$PY" else a for a in argv]
    return run_job(os.path.splitext(log)[0], argv, cwd or project(HW), log,
                   env={"P2_TERV": cfg["p2_terv"], **(env or {})}, **({} if wait is None else {"wait": wait}))


def jload(rel):
    return json.load(open(project(f"{HW}/{rel}")))


def guard(cfg):
    p = paths(cfg)
    r = cfg["round"]
    errs = []
    if not r.startswith("r14") or "/" in r: errs.append(f"bad round name {r}")
    if r in cfg.get("frozen_rounds", []): errs.append(f"{r} is a frozen round")
    for k in ("R", "RC"):
        if os.path.exists(project(f"{HW}/{p[k]}")): errs.append(f"{p[k]} already exists; rounds are write-once")
    if not os.path.isdir(project(f"{HW}/{p['B']}/all")): errs.append(f"input build {p['B']}/all missing")
    if not os.path.isdir(project(f"{HW}/{p['prev_nocbe']}")): errs.append(f"previous round {p['prev_nocbe']} missing")
    if cfg.get("post_only") is not True: errs.append("POST_ONLY must be true: the graph never rebuilds the input build")
    free = shutil.disk_usage(project(HW)).free / 1e9
    if free < 6: errs.append(f"only {free:.1f} GB free")
    return errs, p, free


def frames_cfg(cfg):
    """The round's frames block in the page round's shape: macmini's sites become the poses; panels prev | this round."""
    f = dict(cfg["frames"])
    f.setdefault("folder", f"highways/work/census/frames/fdr_{cfg['round']}")
    f.setdefault("panels", [f.get("pairs_vs", cfg.get("prev", "before")), cfg["round"]])
    f.setdefault("poses", [s["name"] + (f" (tile {s['tile']})" if s.get("tile") else "") + (f": {s['why']}" if s.get("why") else "")
                           for s in f.get("sites", [])])
    return dict(cfg, frames=f, mode=cfg.get("mode", "live"))


def frames_step(cfg, tid):
    """frames_ready: macmini renders the pairs into the folder, writes guards_frames.json and drops READY; G0 (the
    lead's capture guard) must pass before Hydrogen sees anything, else macmini re-shoots (remove READY, re-render, drop
    it again). hydrogen_frames: the page round's per-site review, plus the G0 summary."""
    import page as P
    c = frames_cfg(cfg)
    folder = P.frames_folder(c)
    need = [s["name"] for s in c["frames"].get("sites", [])]   # the lead (r14w): G0 on ALL the round's pairs
    cg = c["frames"].get("capture_guard", {})
    if cg.get("version") == 2:   # the lead's G0 v2 (adapters/g0_v2.py): capture noise first, a repeat clean failure goes to the lead
        import g0_v2

        def g0():   # evaluated only once READY is there, so a half-written file is never counted as a shoot
            state, why, _ = g0_v2.evaluate(folder, need, cg.get("max_diff_pct", 0.05), os.path.join(OUT, "g0_v2_history.json"))
            if state == "lead":
                raise RuntimeError(f"G0 v2: {why} (a lead decision)")
            return state == "pass", (why if state == "pass" else f"G0 v2: {why}")
    else:
        g0 = lambda: _g0(folder, cg.get("max_diff_pct", 0.05), need)
    if tid == "frames_ready":
        try:
            out = P.frames_ready(c)
        except P.Waiting as e:
            raise Running(str(e).replace("from the page editor", "from macmini"))
        ok, why = g0()
        if not ok:
            raise Running(f"awaiting frames from macmini: G0 capture guard failed ({why}); re-shoot both, then drop READY again")
        return {**out, "request": out["request"]}
    ok, why = g0()
    if not ok:
        raise RuntimeError(f"G0 capture guard failed: {why}")
    out = P.hydrogen_frames(c)
    worse = [r for r in json.loads(out.get("rows") or "[]") if r.get("verdict") == "worse"]
    if worse:   # the lead (r14w): a pair Hydrogen calls worse is a lead decision; it never passes on numbers
        raise RuntimeError(f"Hydrogen judged {len(worse)} pair(s) worse: " + "; ".join(
            f"{r['site']}: {'; '.join(r.get('new', []))[:160]}" for r in worse)[:900] + " (a lead decision)")
    return {**out, "capture_guard": why}


def _g0(folder, lim, need=()):
    f = os.path.join(folder, "guards_frames.json")
    if not os.path.exists(f):
        return False, "no guards_frames.json in the frames folder"
    pairs = json.load(open(f)).get("pairs", [])
    from g0_v2 import site_of
    have = {site_of(x.get("pose"), list(need)) for x in pairs} if need else set()
    missing = [n for n in need if n not in have]
    if missing:
        return False, f"no G0 row for {len(missing)} of {len(need)} pairs: {', '.join(missing[:6])}"
    v = lambda x: x.get("g0_pixel_diff_pct", x.get("capture_diff_pct"))
    bad = [x for x in pairs if v(x) is None or v(x) >= lim]
    if not pairs or bad:
        return False, f"{len(bad)} of {len(pairs)} pairs at or over {lim} % on unchanged parts: " + ", ".join(
            f"{x.get('pose')} {v(x)}" for x in bad[:10])
    return True, f"G0 passed: {len(pairs)} pairs under {lim} % on unchanged parts (max {max(v(x) for x in pairs)} %)"


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        return validate(req, c.get("pass", "true") == "true")

    cfg = round_config()
    p = paths(cfg)
    R, RC, r = p["R"], p["RC"], cfg["round"]
    plan_only = cfg.get("mode") == "plan_only"

    if tid in ("frames_ready", "hydrogen_frames"):   # r14w on: the page round's frames hand-off and Hydrogen review
        return respond(req, output=frames_step(cfg, tid))

    if tid == "h_guard":
        errs, p, free = guard(cfg)
        if errs:
            return respond(req, "settled", error="; ".join(errs))
        return respond(req, output={"pass": "true", "writes": json.dumps([f"{HW}/{R}", f"{HW}/{RC}",
                                    f"{HW}/work/p2net/final_fdr/{r}_profile_all.json",
                                    f"{HW}/work/p2net/final_fdr/{r}_profile_solved.json",
                                    f"{HW}/work/p2net/final_fdr/{r}_nocbe_profile_all.json",
                                    f"{HW}/work/p2net/<check>_{r}[_nocbe].json"]), "free_gb": round(free, 1)})
    if plan_only:
        return respond(req, "not_started", error="plan-only dry run: nothing executed")

    if tid == "h_assemble":
        env = {"P2_TAKE_ALL": "1", "P2_TAKE_P2_TILES": "1", "P2_OUT": R, "P2_BUILD": f"{p['B']}/all"}
        steps = [(["$PY", "-I", "work/p2net/assemble_p2.py", "fdr"], env),
                 (["$PY", "-I", "work/p2net/add_lane_attrs.py", R, "work/p2net/w_fdr/profile_all.json"], {}),
                 (["cp", "work/p2net/w_fdr/profile_all.json", f"work/p2net/final_fdr/{r}_profile_all.json"], {}),
                 (["cp", "work/p2net/fdr/profile_solved.json", f"work/p2net/final_fdr/{r}_profile_solved.json"], {}),
                 (["rm", "-f", f"work/p2net/floor_fix_{r}.json"], {})]
        for i, (argv, env) in enumerate(steps):
            rc = run(cfg, argv, f"h_assemble_{i}.log", env)
            if rc: return respond(req, "settled", error=f"assemble step {i} exited {rc}")
        return respond(req, output={"pass": "true"})

    if tid.startswith("h_pass_"):
        s = cfg["passes"][int(tid.split("_")[2])]
        rc = run(cfg, ["$PY", "-I", f"work/p2net/{s}.py", R], f"{tid}_{s}.log", cfg["pass_env"])
        return respond(req, output={"pass": "true", "step": s}) if rc == 0 else respond(req, "settled", error=f"{s} exited {rc}")

    if tid == "h_part_move":
        env = {"KEEP_UP": f"work/p2net/lightwell_{r}.json", "DOWN": cfg["pass_env"]["COVERED"], "UP": cfg["part_move_up"]}
        rc = run(cfg, ["$PY", "-I", "work/p2net/part_move.py", R], "h_part_move.log", env)
        return respond(req, output={"pass": "true"}) if rc == 0 else respond(req, "settled", error=f"part_move exited {rc}")

    if tid.startswith("h_after_"):   # passes after part_move, before the CBE copy (r14u: drive_band_clear)
        st = cfg["after_part_move"][int(tid[len("h_after_"):])]
        rc = run(cfg, ["$PY", "-I", f"work/p2net/{st}.py", R], f"{tid}_{st}.log", cfg["pass_env"])
        if rc != 0:
            return respond(req, "settled", error=f"{st} exited {rc}")
        if st == "drive_band_clear":   # lead's condition (21 Nov): it may remove retaining_wall faces only
            j = jload(f"work/p2net/drive_band_clear_{r}.json")
            other = {k: v for k, v in j.items() if k.startswith("removed_by_material_")
                     and k != "removed_by_material_retaining_wall" and v}
            if other:
                return respond(req, output={"pass": "false", "step": f"{st} removed non-wall material: {other}"})
            n = lambda v: len(v) if isinstance(v, (list, dict)) else v   # some fields are lists of places
            kept = "".join(f", {lbl} {n(j[k])}" for k, lbl in (("kept_step_faces", "step faces kept"),
                                                                ("kept_step_faces_in_wheel_path", "of them in a wheel path"))
                           if k in j)
            return respond(req, output={"pass": "true", "step": f"{st}: {j.get('removed_by_material_retaining_wall', 0)} "
                                                              f"retaining_wall triangles removed, nothing else{kept}"})
        if st == "wall_band_clip":   # r14w: cuts retaining_wall faces only (barriers/railings/medians/parapets never touched)
            j = jload(f"work/p2net/wall_band_clip_{r}.json")
            return respond(req, output={"pass": "true", "step": f"{st}: {j.get('clipped_triangles', 0)} wall triangles cut, "
                                                              f"{j.get('removed_m2', 0)} m2 removed, {j.get('headwall_pieces', 0)} headwall pieces, "
                                                              f"{j.get('kept_by_keep_list', 0)} left uncut by the keep list"})
        return respond(req, output={"pass": "true", "step": st})

    if tid == "h_cbe_copy":
        for t in CBE:
            for f in (f"tile_{t}_above.glb", f"lanes_{t}.json"):
                shutil.copy2(project(f"{HW}/work/hw5/b9/all/{f}"), project(f"{HW}/{R}/{f}"))
        rc = run(cfg, ["cp", "-al", R, RC], "h_nocbe.log")
        if rc: return respond(req, "settled", error=f"nocbe copy exited {rc}")
        for t in CBE:
            for f in (f"tile_{t}_above.glb", f"lanes_{t}.json"):
                fp = project(f"{HW}/{RC}/{f}")
                if os.path.exists(fp): os.remove(fp)   # inside the new round's own _nocbe copy only
        shutil.copy2(project(f"{HW}/work/p2net/final_fdr/{r}_profile_all.json"),
                     project(f"{HW}/work/p2net/final_fdr/{r}_nocbe_profile_all.json"))
        return respond(req, output={"pass": "true"})

    # ---- D: stop checks, pass/fail in code from the scripts' own JSON ----
    if tid == "d_floor_fix_guard":
        ok = os.path.exists(project(f"{HW}/work/p2net/floor_fix_{r}.json"))
        return respond(req, output={"pass": str(ok).lower(), "detail": "floor_fix json present" if ok else "FLOOR_FIX GUARD FAILED"})
    if tid == "d_tile_inventory":
        rc = run(cfg, ["$PY", "-I", "work/p2net/tile_inventory_check.py", R], "d_tile_inventory.log")
        return respond(req, output={"pass": str(rc == 0).lower(), "detail": f"exit {rc}"})
    if tid == "d_no_surface":
        run(cfg, ["$PY", "-I", "work/p2net/no_surface_on_path.py", RC], "d_no_surface.log")
        m = jload(f"work/p2net/no_surface_on_path_{r}_nocbe.json")["no_surface_on_path_m"]
        tot = sum(m.values()) if isinstance(m, dict) else float(m or 0)
        return respond(req, output={"pass": str(tot == 0).lower(), "detail": f"no surface on path {tot} m"})
    if tid == "d_unsupported":
        run(cfg, ["$PY", "-I", "work/p2net/unsupported_check.py", RC, p["prev_nocbe"]], "d_unsupported.log")
        j = jload(f"work/p2net/unsupported_check_{r}_nocbe.json")
        u = j[f"new_vs_{cfg['prev']}_nocbe"]
        dl = list({e["name"]: e for e in cfg.get("debt", []) if e.get("check") == "d_unsupported"}.values())   # the lead's debt (max, not summed)
        lim = max([10] + [e["unsupported_m_max"] for e in dl])
        ok = u["unsupported"]["m_approx"] <= lim and u["hanging"]["samples"] == 0
        # lead (via macmini, 21 Nov): overpass edges (>= 4.6 m over a drivable deck) don't count; the old definition
        # and the moved metres are reported beside the gate
        extra = "".join(f"; {lbl} {j[k]} m" for k, lbl in (("unsupported_deck_edge_m", "unsupported (new definition)"),
                                                           ("unsupported_old_def_m", "old definition"),
                                                           ("overpass_edge_m", "moved to overpass edges (>= 4.6 m clearance)"),
                                                           ("low_clearance_edge_m", "low-clearance edges 1-4.6 m over a deck (still counted)"))
                        if k in j)
        return respond(req, output={"pass": str(ok).lower(),
                                    "detail": f"new unsupported {u['unsupported']['m_approx']} m (max "
                                              + (f"{lim}: the lead's debt allows {lim - 10:g} m over 10 at "
                                                 f"{', '.join(e['name'] for e in dl)}" if dl else "10") + "); new hanging "
                                              f"{u['hanging']['samples']}" + extra})
    if tid == "d_underlay":
        run(cfg, ["$PY", "-I", "work/p2net/underlay_check.py", RC], "d_underlay.log")
        a = jload(f"work/p2net/underlay_check_{r}_nocbe.json")["underlay_above_drivable_m2"]
        return respond(req, output={"pass": str(a == 0).lower(), "detail": f"underlay above drivable {a} m2 (must be 0)"})
    if tid == "d_void":
        for s in ("seethrough_city", "void_classify"):
            rc = run(cfg, ["$PY", "-I", f"work/p2net/{s}.py", RC], f"d_{s}.log")
            if rc: return respond(req, "settled", error=f"{s} exited {rc}")
        run(cfg, ["$PY", "-I", "work/p2net/void_diff.py", f"{cfg['prev']}_nocbe", f"{r}_nocbe"], "d_void_diff.log")
        v = jload(f"work/p2net/void_diff_{cfg['prev']}_nocbe_{r}_nocbe.json")
        reg = v.get("regression_unreasoned_m2", v["regression_real_m2"])   # macmini: unreasoned regression stops
        # the lead's debt: an allowance in m2, one per (check, name), so a re-approved debt can't stack
        dl = list({e["name"]: e for e in cfg.get("debt", []) if e.get("check") == "d_void"}.values())
        allow = sum(e["void_m2_max"] for e in dl)
        return respond(req, output={"pass": str(reg <= allow).lower(),
                                    "detail": f"void regression without a reason {reg} m2 (all {v['regression_real_m2']} m2)"
                                              + (f"; the lead's debt allows {allow} m2 ({', '.join(e['name'] for e in dl)})"
                                                 if dl else "")})
    if tid == "d_walls":
        run(cfg, ["$PY", "-I", "work/p2net/wall_near_lane.py", RC, p["prev_nocbe"]], "d_walls.log")
        w = jload(f"work/p2net/wall_near_lane_{r}_nocbe.json")
        # absolute run against an empty reference (macmini): live's count is the limit; unreasoned rows stop
        run(cfg, ["$PY", "-I", "work/p2net/wall_near_lane.py", RC, "work/p2net/empty_ref"], "d_walls_abs.log")
        base = f"work/p2net/wall_near_lane_{r}_nocbe_abs"
        keep = os.path.join(project(HW), base + "_v3c.json")
        if cfg.get("walls_report_also") and not os.path.exists(keep):   # set v3c aside BEFORE v3a can run (r14x-1's bug)
            shutil.copy2(os.path.join(project(HW), base + ".json"), keep)
        a = json.load(open(keep)) if os.path.exists(keep) else jload(base + ".json")   # v3c, whatever v3a is doing
        v3 = ""
        if cfg.get("walls_report_also"):   # the lead (r14w): report walls on v3 too; v3a writes the same file name
            if not os.path.exists(os.path.join(project(HW), base + "_v3a.json")):
                run(cfg, ["$PY", "-I", cfg["walls_report_also"], RC, "work/p2net/empty_ref"], "d_walls_v3a.log")
                shutil.move(os.path.join(project(HW), base + ".json"), os.path.join(project(HW), base + "_v3a.json"))
                shutil.copy2(keep, os.path.join(project(HW), base + ".json"))
            b = jload(base + "_v3a.json")
            v3 = (f"; walls on v3 ({os.path.basename(cfg['walls_report_also'])}, reported): on-road {b.get('on_road_m')} m / "
                  f"{b.get('on_road_triangles')} triangles")
        wa = cfg.get("walls_abs") or {}
        if wa.get("metric") == "m":   # lead's decision (A), r14v on: absolute gate on metres vs live; triangles reported
            lim = wa["max_m"]
            ok = w["on_road_m"] == 0 and a["on_road_m"] <= lim and (
                not wa.get("unreasoned_gate", False) or a.get("on_road_unreasoned_triangles", 0) == 0)
            det = (f"new on-road walls {w['on_road_m']} m (must be 0); absolute on-road {a['on_road_m']} m (max {lim}, "
                   f"live's); {a['on_road_triangles']} triangles (reported); unreasoned "
                   f"{a.get('on_road_unreasoned_triangles', 'n/a')}" + (" (must be 0)" if wa.get("unreasoned_gate") else "")
                   + (f"; step faces ('{a.get('step_face_class', 'overlapping decks at different heights')}') "
                      f"{a['on_road_step_face_triangles']} triangles / {a.get('on_road_step_face_m', '?')} m, reasoned by class"
                      if "on_road_step_face_triangles" in a else "")) + v3
        else:
            lim = cfg.get("walls_abs_max_triangles", 620)
            ok = w["on_road_m"] == 0 and a["on_road_triangles"] <= lim and a.get("on_road_unreasoned_triangles", 0) == 0
            det = (f"new on-road walls {w['on_road_m']} m (must be 0); absolute on-road {a['on_road_triangles']} triangles "
                   f"(max {lim}); unreasoned {a.get('on_road_unreasoned_triangles', 'n/a')}")
        return respond(req, output={"pass": str(ok).lower(), "detail": det})
    if tid == "d_columns":
        # macmini (21 Nov): stopping. No post on another way's pavement, none across a barrier (r14t: 0 and 0).
        run(cfg, ["$PY", "-I", "work/p2net/column_rules_check.py", RC], "d_columns.log")
        j = jload(f"work/p2net/column_rules_check_{r}_nocbe.json")
        ok = j["column_on_pavement"] == 0 and j["column_x_barrier"] == 0
        return respond(req, output={"pass": str(ok).lower(), "detail": f"posts {j['posts']}, on pavement "
                                    f"{j['column_on_pavement']}, across a barrier {j['column_x_barrier']} (both must be 0)"})
    if tid == "d_tile_diff":
        # the lead (r14t2): list every file of the round that differs from a frozen round's SHA256SUMS (changed, new,
        # missing), each with its reason from the round's reasons file; stops on any without a reason
        td = cfg["tile_diff"]
        sums = {}
        for line in open(project(f"{HW}/work/p2net/final_fdr/{td['vs']}.SHA256SUMS")):
            if line.strip():
                h, name = line.split(None, 1)
                sums[name.strip().lstrip("*")] = h
        folder = project(f"{HW}/{R}")
        now = {f: sha256(os.path.join(folder, f)) for f in sorted(os.listdir(folder)) if os.path.isfile(os.path.join(folder, f))}
        rf = project(f"{HW}/{td['reasons']}") if td.get("reasons") else None
        reasons = json.load(open(rf)) if rf and os.path.exists(rf) else {}
        rows = [(f, "changed") for f in now if f in sums and now[f] != sums[f]] + [(f, "new") for f in now if f not in sums] + \
               [(f, "missing") for f in sums if f not in now]
        why = lambda f: reasons.get(f) or reasons.get(f.split("_above")[0].split("_below")[0].replace("tile_", "").replace(".glb", ""))
        bad = [(f, k) for f, k in rows if not why(f)]
        listing = "; ".join(f"{f} ({k}): {why(f) or 'NO REASON'}" for f, k in rows)
        json.dump([{"file": f, "change": k, "reason": why(f)} for f, k in rows], open(os.path.join(OUT, "tile_diff.json"), "w"),
                  indent=1)   # the full list for the gate summary (the detail below is cut at ~3,800 characters)
        return respond(req, output={"pass": str(not bad).lower(), "detail": (
            f"{len(rows)} files differ from frozen {td['vs']} ({sum(1 for _, k in rows if k == 'changed')} changed, "
            f"{sum(1 for _, k in rows if k == 'new')} new, {sum(1 for _, k in rows if k == 'missing')} missing); "
            f"{len(bad)} without a reason (must be 0): {listing}")[:3800]})
    if tid == "d_lane_void":
        # r14w (lead 21 Nov): driver-height openings within 3 m of a lane edge, new vs the previous round; stops on any
        # new hole without a reason (lane_void_reasons.json)
        run(cfg, ["$PY", "-I", "work/p2net/lane_void_check.py", RC, p["prev_nocbe"]], "d_lane_void.log")
        j = jload(f"work/p2net/lane_void_check_{r}_nocbe.json")
        ok = j["new_unreasoned"] == 0
        return respond(req, output={"pass": str(ok).lower(), "detail": f"new driver-height holes {j['new_cells']} cells, unreasoned "
                                    f"{j['new_unreasoned']} (must be 0); openings onto a lower road (portals, listed) "
                                    f"{j.get('opened_to_road_only_cells', 0)} cells; tiles {len(j['tiles'])}"})
    if tid == "d_crossing":
        run(cfg, ["$PY", "-I", "work/p2net/crossing_check.py", f"work/p2net/final_fdr/{r}_profile_all.json", R], "d_crossing.log")
        c = jload(f"work/p2net/crossing_check_{r}.json")
        bad = {k: c.get(k, 0) for k in ("a_unreasoned", "b_unreasoned", "c_unreasoned")}
        return respond(req, output={"pass": str(not any(bad.values())).lower(), "detail": json.dumps(bad)})
    if tid == "d_listed":
        # Reported, not stopping: run in make-chain order, logs kept in the run folder.
        cmds = [(["$PY", "-I", "work/p2net/underlay_check.py", RC], {"FLOORS": "all", "FLOOR_CLASS": "deck_concrete"}),
                (["$PY", "-I", "work/p2net/underlay_check.py", RC], {"FLOORS": "all", "FLOOR_CLASS": "ground_fill"}),
                (["$PY", "-I", "work/p2net/plan_hole_check.py", RC, f"work/p2net/plan_hole_check_{r}.json"], {})]
        cmds += [(["$PY", "-I", f"work/p2net/{s}.py", RC], {}) for s in cfg["listed_loop"]]
        cmds += [(["$PY", "-I", "work/p2net/cut_check2.py", "1105.8", "2179.6"], {})]
        cmds += [(["$PY", "-I", "work/p2net/covered_lanes.py", R], {})]
        cmds += [(["$PY", "-I", f"work/p2net/{s}.py", RC], {}) for s in
                 ("column_span_check", "step_census", "overlap_count")]   # column_rules_check is the d_columns stop
        cmds += [(["$PY", "-I", "work/p2net/size_by_class.py", "work/hw7/final/all", RC], {})]
        # one job at a time, each call within Irrigator's 120 s: finished jobs are collected, the next one is started
        import time as _t
        t0, rcs = _t.time(), []
        for i, (a, e) in enumerate(cmds):
            rcs.append(run(cfg, a, f"d_listed_{i}.log", e, wait=max(0, 60 - (_t.time() - t0))))
        # page editor's at-grade check: reads its script locally (page code never leaves this machine)
        rcs.append(run(cfg, ["node", project("pe:phaseb/atgrade_check.mjs"), f"highways/{RC}",
                             f"work/terrain_study/p1/terrain/{cfg['p2_terv']}", f"highways/work/p2net/atgrade_{r}.json"],
                       "d_listed_atgrade.log", cwd=os.environ["NYC_ROOT"], wait=max(0, 60 - (_t.time() - t0))))
        return respond(req, output={"pass": "true", "detail": f"{len(cmds)} listed checks, exits {rcs}"})
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    run_main(main)
