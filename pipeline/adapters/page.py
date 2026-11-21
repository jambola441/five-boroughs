#!/usr/bin/env python3
"""Page-editor round nodes: wraps the page editor's EXISTING tools unchanged (spec:
work/page_editor/ROUND_GRAPH_SPEC_PAGE_EDITOR.md, steps 1-8), as detached jobs.

Hard rules (the page editor's):
- No step builds, serves, reads, copies or passes on the decrypted page (page_editor/src) or any built page.
  The tools are called by local path only; nothing from page_editor/phaseb is copied anywhere.
- agy (Hydrogen) sees only the game frames the page editor rendered, copied into a folder of their own.
- Each step checks free disk first and pauses below 10 GB; it writes only to its own new output folder.

mode "existing" = dry run: every step reads the output the round file names under "existing" and runs nothing.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import datetime
import glob
import json
import math
import os
import re
import shutil

from common import (coordinator_json, deps, load_request, project, respond, round_config, run_job, run_main,
                    validate, OUT)

W = "work"
PE, DA = f"{W}/page_editor", f"{W}/drive_audit"
NODE = "/opt/homebrew/bin/node"
MIN_FREE_GB = 10
IMG = (".jpg", ".jpeg", ".png")


class Waiting(Exception):
    """An input or output is not there yet: the step reports not_started and is retried later."""


def py():
    return os.path.join(os.environ["NYC_ROOT"], ".venv/bin/python")


def step_cfg(cfg, tid):
    for s in cfg["steps"]:
        if s["id"] == tid:
            return s
    raise KeyError(tid)


def disk_ok():
    free = shutil.disk_usage(project(".")).free / 1e9
    if free < MIN_FREE_GB:
        raise Waiting(f"only {free:.1f} GB free; need {MIN_FREE_GB} GB (paused)")
    return round(free, 1)


def ref(cfg, d, v):
    """A path in the round file: 'step:<id>' = that step's output file; else a project or runs: path."""
    if v.startswith("step:"):
        sid = v[len("step:"):]
        if sid not in d:
            raise Waiting(f"needs {sid} first")
        return d[sid]["out"]
    return project(v)


def node_env(s):
    """The switches the spec names, passed through to the node tools exactly as the round file sets them."""
    env = {k: str(s[k]) for k in ("TERV", "LIFTRULE", "RESLOCAL", "ONDECK", "LANES", "POLYLIFT") if k in s}
    if "LANES" in env:
        env["LANES"] = project(env["LANES"])
    return env


def existing_or_run(cfg, s, out, launch):
    """Dry run: the named existing output must be there. Live: run the tool once into a new output."""
    if cfg["mode"] == "existing" or s.get("use_existing"):   # use_existing: reuse an earlier output in a live round
        p = project(s["existing"]) if "existing" in s else None
        if not p or not os.path.exists(p):
            raise Waiting(f"awaiting {s['id']} output (dry run; nothing re-run): {s.get('existing', 'none named')}")
        return p
    if os.path.exists(out) and not os.path.exists(os.path.join(OUT, "jobs", s["id"], "job.json")):
        raise RuntimeError(f"{os.path.basename(out)} already exists; outputs are write-once, pick a new tag")
    if not os.path.exists(os.path.join(OUT, "jobs", s["id"], "job.json")):
        disk_ok()
    rc = launch()
    if rc != 0:
        raise RuntimeError(f"{s['id']} exited {rc}; see {s['id']}.log")
    return out


# ---------- steps (spec numbers) ----------

def t_drape(cfg, s, d):
    """1. overlay drape of a variant, then symlink every base tile it did not write."""
    out = project(f"{DA}/page_drape/{s['tag']}")
    base = project(f"{DA}/page_drape/{s['base']}")
    script = ("env ONLYDIFF=\"$BASE\" \"$NODE\" --import ../page_editor/phaseb/fb_env_preload.mjs "
              "../page_editor/phaseb/da_page_drape_wc.mjs page_drape/$TAG page_drape/tiles.json && "
              "for f in \"$BASE\"/*.drape.json; do n=$(basename \"$f\"); [ -e page_drape/$TAG/$n ] || "
              "ln -s \"$f\" page_drape/$TAG/$n; done")
    p = existing_or_run(cfg, s, out, lambda: run_job(s["id"], ["/bin/sh", "-c", script], project(DA), s["id"] + ".log",
                                                     env={**node_env(s), "BASE": base, "TAG": s["tag"], "NODE": NODE}))
    files = glob.glob(os.path.join(p, "*.drape.json"))
    written = [f for f in files if not os.path.islink(f)]
    return {"out": p, "tiles_written": len(written), "tiles_total": len(files)}


GATE_RX = {
    "visible_drivable": r"GATE visible drivable >= 2 m: (\d+) \(live (\d+)\); new beyond live: (\d+)",
    "lost_contact": r"lost-contact flags on CSCL car routes (\d+) \(live (\d+)\); new vs live at the same point \(3 m\): (\d+) flags at (\d+) places",
    "city": r"car ledges (\d+) >= 2 m (\d+) \| car steps >= 2 m v2 (\d+)",
}


def gate_txt(folder):
    t = open(os.path.join(folder, "GATE.txt")).read()
    g = {k: [int(x) for x in m.groups()] for k, rx in GATE_RX.items() if (m := re.search(rx, t))}
    lines = [l for l in t.splitlines() if l.startswith(("GATE", "RAMPGATE"))]
    return g, lines


def t_lift_gate(cfg, s, d):
    """2. the drive test's run_lift_gate.sh --city, unchanged."""
    drape = ref(cfg, d, s["drape"]) if s.get("drape") else project(f"{DA}/page_drape/{s['tag']}")
    out = project(f"{DA}/deckends/lift_pd_{s['tag']}")
    env = {"RAMPS": str(s.get("RAMPS", 0)), "GATE_HW": project(s["LANES"]), "DA_PAGE_DRAPE": drape,
           "DA_YE_DRAWN": "0", "DA_SCAN_DRAWN": "1"}
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], ["./run_lift_gate.sh", project(f"{W}/terrain_study/p1/terrain/{s['TERV']}"), "pd_" + s["tag"], "--city"],
        project(DA), s["id"] + ".log", env=env))
    g, lines = gate_txt(p)
    return {"out": p, "flags": os.path.join(p, "city", "flags_drape.json"), "visible_drivable_new": g["visible_drivable"][2],
            "lost_contact_flags": g["lost_contact"][0], "lost_contact_new_places": g["lost_contact"][3],
            "car_steps_2m": g.get("city", [0, 0, -1])[2], "gate_lines": json.dumps(lines[:12])}


def t_newlist(cfg, s, d):
    """3. new and gone places vs the base city scan (city_new_list.mjs)."""
    base = ref(cfg, d, s["base_flags"])
    var = d[s["variant_flags"][5:]]["flags"] if s["variant_flags"].startswith("step:") else project(s["variant_flags"])
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE, "--max-old-space-size=12000", project(f"{PE}/phaseb/city_new_list.mjs"), base, var, out],
        project(PE), s["id"] + ".log"))
    j = json.load(open(p))
    return {"out": p, "new_places": j["newInY"]["places"], "new_step2": j["newInY"]["step2"],
            "new_lost": j["newInY"]["lost"], "gone_places": j["goneInY"]["places"], "gone_step2": j["goneInY"]["step2"],
            "gone_lost": j["goneInY"]["lost"]}


def t_deck_under(cfg, s, d):
    """4. the mid-deck guard (deck_under.mjs)."""
    drape = ref(cfg, d, s["drape"])
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE, "--max-old-space-size=8192", project(f"{PE}/phaseb/deck_under.mjs"), drape, out],
        project(PE), s["id"] + ".log", env=node_env(s)))
    j = json.load(open(p))
    o = {"out": p, **{k: j["total"][k] for k in ("underM", "noneM", "linesUnder")}, "zero_piece_underM": "none"}
    z = cfg.get("deck_zero_piece")   # the #6 piece: must show 0 m under
    if z:
        near = [r for r in j["rows"] if r["name"] == z["name"]
                and min(math.hypot(r["start"][0] - z["x"], r["start"][1] - z["z"]),
                        math.hypot(r["end"][0] - z["x"], r["end"][1] - z["z"])) <= z.get("radius_m", 15)]
        o["zero_piece_underM"] = round(sum(r["underM"] for r in near), 1)
    return o


def t_ramps(cfg, s, d):
    """5. ramp census (run_ramp_score.sh), RAMPGATE vs the drive test's FLATTENED_r6r_r14s.json."""
    out = project(f"{DA}/deckends/ramps_{s['tag']}")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [project(f"{PE}/phaseb/run_ramp_score.sh"), s["tag"]], project(DA), s["id"] + ".log",
        env={"RAMP_TERV": s["TERV"]}))
    t = open(os.path.join(p, "gate.log")).read()
    m = re.search(r"RAMPGATE \S+: flattened (\d+) \(previous (\d+)\); new (\d+), gone (\d+) -> (\w+)", t)
    if not m:
        raise RuntimeError("no RAMPGATE line in gate.log")
    f, prev, new, gone, verdict = m.groups()
    return {"out": p, "flattened": int(f), "previous": int(prev), "new": int(new), "gone": int(gone),
            "rampgate": verdict, "line": m.group(0)}


def t_trace(cfg, s, d):
    """7. per-site claim trace (trace_sites.mjs); reported only."""
    lst = ref(cfg, d, s["list"])
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE, project(f"{PE}/phaseb/trace_sites.mjs"), lst, out], project(PE), s["id"] + ".log",
        env=node_env(s)))
    j = json.load(open(p))
    return {"out": p, "sites": len(j["rows"]), "counts": json.dumps(j["counts"])}


def t_lidar(cfg, s, d):
    """6. LiDAR restore check (lidar_restore_check.py); reported only. Local EPT cache, read-only."""
    lst, drape = ref(cfg, d, s["list"]), ref(cfg, d, s["drape"])
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project(f"{PE}/phaseb/lidar_restore_check.py"), lst, drape, out], project(PE),
        s["id"] + ".log"))
    j = json.load(open(p))
    cls = {}
    for r in j["rows"]:   # C (LiDAR matches neither drawn side) split here, the tool doesn't: C-other = LiDAR has a
        c = r.get("cls", "?")   # continuous road layer at some other height; C-none = no continuous LiDAR road there
        if c == "C":
            c = "C-none" if not r.get("lidar_cont") else "C-other"
        cls[c] = cls.get(c, 0) + 1
    return {"out": p, "sites": len(j["rows"]), "counts": json.dumps(j["counts"]), "classes": json.dumps(cls)}


def t_census(cfg, s, d):
    """8. plan-polygon census (bridge_own_census.mjs): tents, overflow."""
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE, "phaseb/bridge_own_census.mjs", "frames/bridgeown/bridge_foot.json", out], project(PE),
        s["id"] + ".log", env={"ALLTILES": "1", **node_env(s)}))
    j = json.load(open(p))
    return {"out": p, "tents_n": j["tents"]["n"], "tents_m2": j["tents"]["m2"], "overflow_polys": j["overflow"]["polys"]}


TOOLS = {"drape": t_drape, "lift_gate": t_lift_gate, "newlist": t_newlist, "deck_under": t_deck_under,
         "ramps": t_ramps, "trace": t_trace, "lidar": t_lidar, "census": t_census}


# ---------- gates (computed in code; budgets from the round file, default 0 = must not rise) ----------

def near(p, pts, r):
    return any(math.hypot(p["x"] - q["x"], p["z"] - q["z"]) <= q.get("radius_m", r) for q in pts)


def gate(cfg, g, d):
    b = cfg.get("budget", {})
    k = g["check"]
    if k == "newlist":
        n = d[g["step"]]
        j = json.load(open(n["out"]))
        new = [i for i in j["newList"] if i.get("c") == "step2" and i.get("mag", 0) >= g.get("min_m", 2)]
        debt = [i for i in new if near(i, cfg.get("debt", []), 30)]
        unb = [i for i in new if i not in debt]
        held = [t["name"] for t in cfg.get("target_fixed", []) if near(t, [dict(x, radius_m=15) for x in j["goneList"]], 15)]
        ok = len(unb) <= b.get("new_step_places", 0) and len(held) == len(cfg.get("target_fixed", []))
        return ok, (f"{len(new)} new car steps >= {g.get('min_m', 2)} m ({len(debt)} on declared debt, {len(unb)} "
                    f"unbudgeted, budget {b.get('new_step_places', 0)}); target fixes held {len(held)} of "
                    f"{len(cfg.get('target_fixed', []))}"), {"unbudgeted": [f"{i.get('rname')} {i['mag']} m at "
                                                              f"{i['x']:.0f},{i['z']:.0f}" for i in unb][:30]}
    if k == "deck_under":
        v, base = d[g["step"]], d[g["base_step"]] if g.get("base_step") else {"underM": g["base_underM"]}
        lim = base["underM"] + b.get("deck_under_m", 0)
        zp = v.get("zero_piece_underM")
        ok = v["underM"] <= lim and zp in ("none", 0)
        return ok, f"underM {v['underM']} vs base {base['underM']} (limit {lim}); #6 piece {zp} m under (must be 0)", {}
    if k == "ramps":
        v = d[g["step"]]
        ok = v["flattened"] <= v["previous"] + b.get("ramps_flattened", 0) and v["new"] <= b.get("ramps_new", 0)
        return ok, f"{v['line']} (budget flattened +{b.get('ramps_flattened', 0)}, new {b.get('ramps_new', 0)})", {}
    if k == "census":
        v, base = d[g["step"]], d[g["base_step"]]
        ok = (v["tents_n"] <= base["tents_n"] + b.get("tents_n", 0)
              and v["tents_m2"] <= base["tents_m2"] + b.get("tents_m2", 0))
        return ok, f"tents {v['tents_n']} / {v['tents_m2']} m² vs base {base['tents_n']} / {base['tents_m2']} m²", {}
    if k == "lift":
        v, base = d[g["step"]], d[g["base_step"]]
        rows = [(f, v[f], base[f], b.get(f, 0)) for f in ("visible_drivable_new", "lost_contact_flags")]
        ok = all(x <= y + bud for _, x, y, bud in rows)
        return ok, "; ".join(f"{f} {x} vs base {y} (budget +{bud})" for f, x, y, bud in rows), {}
    raise KeyError(k)


# ---------- frames hand-off and Hydrogen ----------

def step_out(cfg, sid):
    s = step_cfg(cfg, sid)
    if cfg["mode"] == "existing" or s.get("use_existing"):
        return project(s["existing"])
    return os.path.join(OUT, sid + ".json")


def frames_folder(cfg):
    return project(cfg["frames"]["folder"])


def frames_ready(cfg):
    """The graph asks for frames; the page-editor session renders them into the named folder and drops READY."""
    f = cfg["frames"]
    folder = frames_folder(cfg)
    req = {"round": cfg["round"], "folder": f["folder"], "poses": f.get("poses", []), "variants": f.get("variants", []),
           "panels": f.get("panels", []), "note": "render into folder; write an empty READY file when done"}
    sites = {}
    for sid in f.get("pick_from", []):   # up to per_class sites of each trace class, biggest step first
        if os.path.exists(os.path.join(OUT, sid + ".json")) or cfg["mode"] == "existing":
            try:
                rows = json.load(open(step_out(cfg, sid)))["rows"]
            except (OSError, KeyError, ValueError):
                continue
            for r in sorted(rows, key=lambda r: -r.get("mag", 0)):
                lst = sites.setdefault(r.get("cls", "?"), [])
                if len(lst) < f.get("per_class", 4):
                    lst.append({"from": sid, "x": r["x"], "z": r["z"], "mag": r.get("mag"), "rname": r.get("rname")})
    if sites:
        req["sites_by_class"] = sites
    json.dump(req, open(os.path.join(OUT, "frames_request.json"), "w"), indent=1)
    imgs = sorted(x for x in os.listdir(folder) if x.lower().endswith(IMG)) if os.path.isdir(folder) else []
    if not os.path.exists(os.path.join(folder, "READY")):
        raise Waiting(f"awaiting frames from the page editor: {len(imgs)} images so far, no READY yet")
    if not imgs:
        raise RuntimeError("READY but no images in the frames folder")
    return {"images": len(imgs), "request": json.dumps(req)[:1500]}


PAGE_ASK = ("These are frames from a 3D city driving game. Each image shows the same camera view side by side; "
            "the panels, left to right, are: {panels}. In each panel list the visible rendering defects (missing "
            "road, floating or misplaced geometry, steps or drops in the road surface, walls or slabs where they "
            "should not be, gaps, see-through holes, z-fighting). Say which defects of the FIRST panel are fixed in "
            "the later panels and which are new there, and where in the frame. End with exactly one JSON block:\n"
            "```json\n{{\"fixed_vs_first\": [\"...\"], \"new_vs_first\": [\"...\"], "
            "\"verdict\": \"better|same|worse|mixed\"}}\n```")


def hydrogen_frames(cfg):
    from qa import hydrogen_review
    folder = frames_folder(cfg)
    panels = ", ".join(cfg["frames"].get("panels") or ["before", "after"])
    rows = []
    for n in sorted(x for x in os.listdir(folder) if x.lower().endswith(IMG)):
        tag = "page_" + re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(n)[0])
        v = hydrogen_review(tag, [(n, os.path.join(folder, n))], "", ask=PAGE_ASK.format(panels=panels))
        rows.append({"frame": n, "verdict": v.get("verdict", ""), "fixed": v.get("fixed_vs_first", []),
                     "new": v.get("new_vs_first", []), "seconds": v["seconds"]})
    json.dump(rows, open(os.path.join(OUT, "hydrogen_frames.json"), "w"), indent=1)
    by = {}
    for r in rows:
        by[r["verdict"] or "none"] = by.get(r["verdict"] or "none", 0) + 1
    return {"frames": len(rows), "verdicts": json.dumps(by), "rows": json.dumps(rows)[:6000]}


# ---------- accept ----------

SCHEMA = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["accept", "reject", "keep_exploring", "stop"]},
    "reasons": {"type": "array", "items": {"type": "string"}},
    "class_rows": {"type": "array", "items": {"type": "object", "properties": {
        "class": {"type": "string"}, "count": {"type": "string"}, "status": {"type": "string"}},
        "required": ["class", "count", "status"]}},
    "next_steps": {"type": "array", "items": {"type": "string"}}},
    "required": ["decision", "reasons", "class_rows", "next_steps"]}
OK_DECISIONS = ("accept", "keep_exploring")


def page_accept(req, cfg, d):
    gates = {k: v for k, v in d.items() if k.startswith("g_")}
    rules_pass = all(v.get("pass") == "true" for v in gates.values())
    expired = bool(cfg.get("expires")) and datetime.date.today() > datetime.date.fromisoformat(cfg["expires"])
    rules_pass = rules_pass and not expired
    brief = {k: cfg[k] for k in ("idea", "question", "budget", "debt", "target_fixed", "expires") if k in cfg}
    ev = {k: {kk: vv for kk, vv in v.items() if kk != "out"} for k, v in d.items()}   # no local paths to the model
    prompt = (open(os.path.join(os.path.dirname(__file__), "..", "prompts", f"page_accept_{cfg['accept']}.md")).read()
              + "\n\nRound: " + cfg["round"] + "\n\nThe round as declared up front:\n" + json.dumps(brief, indent=1)
              + f"\n\nGate verdict, computed in code: {'PASS' if rules_pass else 'FAIL'}"
              + (" (past its expiry date)" if expired else "")
              + "\n\nEvidence:\n" + json.dumps(ev, indent=1)[:16000])
    try:
        v = coordinator_json(prompt, SCHEMA, cwd=OUT, spec=cfg.get("coordinator"))
    except Exception as e:
        return respond(req, "uncertain", error=f"coordinator failed: {e}"[:300])
    json.dump(v, open(os.path.join(OUT, f"page_accept_a{req.get('attempt', 1)}.json"), "w"), indent=1)
    return respond(req, output={"rules_pass": str(rules_pass).lower(), "decision": v["decision"],
                                "reasons": json.dumps(v["reasons"]), "class_rows": json.dumps(v["class_rows"]),
                                "next_steps": json.dumps(v["next_steps"])})


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        if tid.startswith("g_"): return validate(req, c.get("pass") == "true")
        if tid == "page_accept": return validate(req, c.get("rules_pass") == "true" and c.get("decision") in OK_DECISIONS)
        if tid == "hydrogen_frames": return validate(req, int(c.get("frames", 0)) > 0)
        return validate(req, True)

    cfg = round_config()
    d = {k: {kk: (json.loads(vv) if isinstance(vv, str) and re.fullmatch(r"-?\d+(\.\d+)?", vv) else vv)
             for kk, vv in v.items()} for k, v in deps(req).items()}
    try:
        if tid == "p_disk":
            free = shutil.disk_usage(project(".")).free / 1e9
            if free < MIN_FREE_GB and cfg["mode"] != "existing":
                raise Waiting(f"only {free:.1f} GB free; need {MIN_FREE_GB} GB (paused)")
            return respond(req, output={"free_gb": round(free, 1)})
        if tid.startswith("p_"):
            s = step_cfg(cfg, tid)
            return respond(req, output=TOOLS[s["tool"]](cfg, s, d))
        if tid.startswith("g_"):
            g = next(x for x in cfg["gates"] if x["id"] == tid)
            ok, why, extra = gate(cfg, g, d)
            return respond(req, output={"pass": str(bool(ok)).lower(), "detail": why,
                                        "items": json.dumps(extra.get("unbudgeted", []))})
        if tid == "frames_ready":
            return respond(req, output=frames_ready(cfg))
        if tid == "hydrogen_frames":
            return respond(req, output=hydrogen_frames(cfg))
        if tid == "page_accept":
            return page_accept(req, cfg, d)
    except Waiting as e:
        return respond(req, "not_started", error=str(e))
    except (RuntimeError, KeyError, ValueError, OSError) as e:
        return respond(req, "settled", error=f"{type(e).__name__}: {e}"[:300])
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    run_main(main)
