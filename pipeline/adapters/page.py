#!/usr/bin/env python3
"""Page-editor round nodes: wraps the page editor's EXISTING tools unchanged (its round-graph spec, kept local;
steps 1-8), as detached jobs. Its folder is reached only through PE_ROOT ('pe:' paths).

Hard rules (the page editor's):
- No step builds, serves, reads, copies or passes on the decrypted page or any built page.
  The tools are called by local path only; none of the page editor's code is copied anywhere.
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
PE, DA = "pe:", f"{W}/drive_audit"
NODE = "/opt/homebrew/bin/node"
MIN_FREE_GB = 10
IMG = (".jpg", ".jpeg", ".png")
HYD_PARALLEL = 8   # Hydrogen calls at once


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
        if sid in d:
            return d[sid]["out"]
        # a finished step this one doesn't depend on in the plan: read its output from the run state
        try:
            t = json.load(open(os.path.join(OUT, "run", "state.json")))["tasks"][sid]
            if t.get("status") == "verified" and (t.get("result") or {}).get("out"):
                return t["result"]["out"]
        except (OSError, ValueError, KeyError):
            pass
        raise Waiting(f"needs {sid} first")
    return project(v)


def node_env(s):
    """The switches the spec names, passed through to the node tools exactly as the round file sets them."""
    env = {k: str(s[k]) for k in ("TERV", "LIFTRULE", "RESLOCAL", "ONDECK", "LANES", "POLYLIFT", "RS2KEEP")
           + tuple(s.get("extra_switches", ())) if k in s}
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
    base = project(f"{DA}/page_drape/{s['base']}") if s.get("base") else ""   # not needed when reusing a drape
    script = ("env ONLYDIFF=\"$BASE\" \"$NODE\" --import \"$PE_ROOT/phaseb/fb_env_preload.mjs\" "
              "\"$PE_ROOT/phaseb/da_page_drape_wc.mjs\" page_drape/$TAG page_drape/tiles.json && "
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
    "curbs": r"CURBS \(deck meets grade, 0\.3-2 m\) (\d+) flags = (\d+) m",
    "c_ii": r"GATE\(c\) vs live split \(ii\) (\d+) m at (\d+) places",
}


def gate_txt(folder):
    t = open(os.path.join(folder, "GATE.txt")).read()
    g = {k: [int(x) for x in m.groups()] for k, rx in GATE_RX.items() if (m := re.search(rx, t))}
    lines = [l for l in t.splitlines() if l.startswith(("GATE", "RAMPGATE"))]
    return g, lines


def zero_piece_m(j, z):
    """metres drawn under the deck lines named z (name + a point within radius of one of the line's ends)."""
    near = [r for r in j["rows"] if r["name"] == z["name"]
            and min(math.hypot(r["start"][0] - z["x"], r["start"][1] - z["z"]),
                    math.hypot(r["end"][0] - z["x"], r["end"][1] - z["z"])) <= z.get("radius_m", 15)]
    return round(sum(r["underM"] for r in near), 1)


def t_lift_gate(cfg, s, d):
    """2. the drive test's run_lift_gate.sh --city, unchanged."""
    drape = ref(cfg, d, s["drape"]) if s.get("drape") else project(f"{DA}/page_drape/{s['tag']}")
    out = project(f"{DA}/deckends/lift_pd_{s['tag']}")
    env = {"RAMPS": str(s.get("RAMPS", 0)), "GATE_HW": project(s["LANES"]), "DA_PAGE_DRAPE": drape,
           "DA_YE_DRAWN": "0", "DA_SCAN_DRAWN": "1"}
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], ["./run_lift_gate.sh", project(f"{W}/terrain_study/p1/terrain/{s['TERV']}"), "pd_" + s["tag"], "--city"],
        project(DA), s["id"] + ".log", env=env, cores=6))   # da_city.py --workers=6: shares the heavy-job limit
    g, lines = gate_txt(p)
    return {"out": p, "flags": os.path.join(p, "city", "flags_drape.json"), "visible_drivable_new": g["visible_drivable"][2],
            "lost_contact_flags": g["lost_contact"][0], "lost_contact_new_places": g["lost_contact"][3],
            "car_steps_2m": g.get("city", [0, 0, -1])[2], "car_ledges_2m": g.get("city", [0, -1, 0])[1],
            "curbs_km": round(g["curbs"][1] / 1000, 3) if "curbs" in g else -1,
            "c_ii_m": g["c_ii"][0] if "c_ii" in g else -1, "gate_lines": json.dumps(lines[:12])}


def t_newlist(cfg, s, d):
    """3. new and gone places vs the base city scan (city_new_list.mjs)."""
    base = ref(cfg, d, s["base_flags"])
    var = d[s["variant_flags"][5:]]["flags"] if s["variant_flags"].startswith("step:") else project(s["variant_flags"])
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE, "--max-old-space-size=12000", project(f"pe:phaseb/city_new_list.mjs"), base, var, out],
        project("pe:"), s["id"] + ".log"))
    j = json.load(open(p))
    return {"out": p, "new_places": j["newInY"]["places"], "new_step2": j["newInY"]["step2"],
            "new_lost": j["newInY"]["lost"], "gone_places": j["goneInY"]["places"], "gone_step2": j["goneInY"]["step2"],
            "gone_lost": j["goneInY"]["lost"]}


def t_deck_under(cfg, s, d):
    """4. the mid-deck guard (deck_under.mjs)."""
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE, "--max-old-space-size=8192", project(f"pe:phaseb/deck_under.mjs"), ref(cfg, d, s["drape"]), out],
        project("pe:"), s["id"] + ".log", env=node_env(s)))
    j = json.load(open(p))
    o = {"out": p, **{k: j["total"][k] for k in ("underM", "noneM", "linesUnder")}, "zero_piece_underM": "none"}
    zs = cfg.get("deck_zero_pieces") or ([cfg["deck_zero_piece"]] if cfg.get("deck_zero_piece") else [])
    if zs:   # each named piece must show 0 m under; the step reports their total, the gate lists each
        tot = 0.0
        for z in zs:
            tot += zero_piece_m(j, z)
        o["zero_piece_underM"] = round(tot, 1)
    return o


def t_ramps(cfg, s, d):
    """5. ramp census (run_ramp_score.sh), RAMPGATE vs the drive test's FLATTENED_r6r_r14s.json."""
    out = project(f"{DA}/deckends/ramps_{s['tag']}")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [project(f"pe:phaseb/run_ramp_score.sh"), s["tag"]], project(DA), s["id"] + ".log",
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
        s["id"], [NODE, project(f"pe:phaseb/trace_sites.mjs"), lst, out], project("pe:"), s["id"] + ".log",
        env=node_env(s)))
    j = json.load(open(p))
    return {"out": p, "sites": len(j["rows"]), "counts": json.dumps(j["counts"])}


def t_lidar(cfg, s, d):
    """6. LiDAR restore check (lidar_restore_check.py); reported only. Local EPT cache, read-only."""
    lst, drape = ref(cfg, d, s["list"]), ref(cfg, d, s["drape"])
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project(f"pe:phaseb/lidar_restore_check.py"), lst, drape, out], project("pe:"),
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
    """8. plan-polygon census (bridge_own_census.mjs): tents, overflow. With rs_events, the page editor's
    rs_events.mjs preload also logs every resolve() rule-2 whole loss (RS_OUT jsonl), counted here."""
    out = os.path.join(OUT, s["id"] + ".json")
    rs = os.path.join(OUT, s["id"] + "_rs.jsonl")
    rk = os.path.join(OUT, s["id"] + "_rk.jsonl")   # with RS2KEEP on: every kept claim (rk_events.mjs)
    keep = bool(s.get("rs_events") and str(s.get("RS2KEEP", "0")) in ("1", "2"))   # 2 = keep + >= 5 m² area
    pre = (["--import", "./phaseb/rs_events.mjs"] if s.get("rs_events") else []) + (["--import", "./phaseb/rk_events.mjs"] if keep else [])
    env = {"ALLTILES": "1", **node_env(s), **({"CLAIMTRACE": "1", "RS_OUT": rs} if s.get("rs_events") else {}),
           **({"RK_OUT": rk} if keep else {})}
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE] + pre + ["phaseb/bridge_own_census.mjs", "frames/bridgeown/bridge_foot.json", out], project("pe:"),
        s["id"] + ".log", env=env))
    j = json.load(open(p))
    o = {"out": p, "tents_n": j["tents"]["n"], "tents_m2": j["tents"]["m2"], "overflow_polys": j["overflow"]["polys"]}
    if s.get("rs_events"):   # tool "census_rs": also counts the rule-2 whole losses
        o.update({"rs_whole_losses": -1, "rs_keep_test": -1})
    if s.get("rs_events") and os.path.exists(rs):
        ev = [json.loads(l) for l in open(rs) if l.strip()]
        whole = [e for e in ev if e["loser"].get("whole")]
        test = [e for e in whole if e["loser"]["score"] >= 0.5
                and set(map(str, e["loser"].get("chain") or [])) & set(map(str, e["winner"].get("chain") or []))]
        o.update({"rs_whole_losses": len(whole), "rs_keep_test": len(test)})
    return o


def t_kept_lidar(cfg, s, d):
    """9. the lead's kept-claim LiDAR guard: the page editor's lidar_kept_check.py on the claims the rule keeps
    (rk_events.mjs log of the RS2KEEP census), variant drape vs base drape; only points the rule raised are judged.
    Classes: PASS / FAIL / NOLIFT (nothing raised) / D (fewer than 3 usable points)."""
    rk = os.path.splitext(d[s["census"]]["out"])[0] + "_rk.jsonl"
    out = os.path.join(OUT, s["id"] + ".json")
    def launch():
        if not os.path.exists(rk):
            raise RuntimeError("kept-claim log missing (the RS2KEEP census must run with rk_events)")
        return run_job(s["id"], [py(), "-I", project("pe:phaseb/lidar_kept_check.py"), rk, ref(cfg, d, s["drape"]), out,
                                 project(s["base_drape"])], project("pe:"), s["id"] + ".log")
    p = existing_or_run(cfg, s, out, launch)
    j = json.load(open(p))
    return {"out": p, "kept": len(j["rows"]), "counts": json.dumps(j["counts"])}


def t_tent_diff(cfg, s, d):
    """10. drape-wide tent census, variant vs base (the page editor's tent_diff.py): every tile the variant wrote itself,
    no bridge radius. A tent = a drawn roadbed / sidewalk triangle spanning > 6 m in y."""
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project("pe:phaseb/tent_diff.py"), ref(cfg, d, s["drape"]), project(s["base_drape"]), out],
        project("pe:"), s["id"] + ".log"))
    t = json.load(open(p))["total"]
    return {"out": p, "n_new": t["n_new"], "m2_new": t["m2_new"], "tiles_changed": t.get("tiles_changed", -1)}


def t_modelled_census(cfg, s, d):
    """modelled-chain census (pe:diag/modelled_census.py): deck lines that count as 'modelled' on their own merits
    ('self'), only through a lane-hugging chain neighbour ('inherited'), or not at all ('free')."""
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project(s.get("script", "pe:diag/modelled_census.py")), project(s["terrain"]),
                  project(s["LANES"]), out]
        + split_args(s), project("pe:"), s["id"] + ".log"))
    j = json.load(open(p))
    S, R2 = j["summary"], j.get("inherited_by_r2") or {}
    g = lambda k, f: (S.get(k) or {}).get(f, 0)
    inh = [r for r in j.get("rows", []) if r.get("cls") == "inherited"]
    # split 3 (MODSPLIT=3, the class rule): an inherited line is a city street by its own OSM highway class
    city = lambda r: r["city_street"] if "city_street" in r else bool(CITY_HW.match(r.get("hw") or ""))
    known = ("inherited_by_class" in j or (bool(inh) and all("city_street" in r or "hw" in r for r in inh))
             or (not inh and s.get("split") == 3))
    st = [r for r in inh if city(r)] if known else []
    ot = [r for r in inh if not city(r)] if known else []
    m = lambda rs: round(sum(r.get("len", 0) for r in rs))
    return {"out": p, "inherited_lines": g("inherited", "lines"), "inherited_m": g("inherited", "m"),
            "self_lines": g("self", "lines"), "self_m": g("self", "m"), "free_lines": g("free", "lines"),
            "free_m": g("free", "m"), "inherited_by_r2": json.dumps(R2),
            "inherited_street_lines": len(st) if known else -1, "inherited_street_m": m(st) if known else -1,
            "inherited_other_lines": len(ot) if known else -1, "inherited_other_m": m(ot) if known else -1,
            "inherited_by_class": json.dumps(j.get("inherited_by_class") or {}),
            "freed_rows": json.dumps([{k: r.get(k) for k in ("name", "osm", "hw", "len")} for r in j.get("freed_rows", [])])[:4000]}


CITY_HW = re.compile(r"^(residential|tertiary|secondary|primary|unclassified|service|living_street|pedestrian)(_link)?$")


def split_args(s):
    """--split for MODSPLIT=1/2 (True, 1 or 2); split 3 needs the page editor's class-rule flag, named in the round
    file as split_flag (its current --split would count MODSPLIT=2's split: a false pass under MODSPLIT=3)."""
    sp = s.get("split")
    if not sp:
        return []
    if sp == 3:   # the page editor's class-rule mode: modelled_census_s3.py --split3 (or split_flag)
        if not s.get("split_flag") and "modelled_census_s3" not in s.get("script", ""):
            raise RuntimeError(f"{s['id']}: split 3 needs script pe:diag/modelled_census_s3.py (or split_flag)")
        f = s.get("split_flag", "--split3")
        return [f] if isinstance(f, str) else list(f)
    return ["--split"]


def t_census_fp(cfg, s, d):
    """census with the page editor's fp_events.mjs preload: every polygon's final ride, logged to <id>_fp.jsonl."""
    out = os.path.join(OUT, s["id"] + ".json")
    fp = os.path.join(OUT, s["id"] + "_fp.jsonl")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [NODE, "--import", "./diag/fp_events.mjs", "phaseb/bridge_own_census.mjs",
                  "frames/bridgeown/bridge_foot.json", out], project("pe:"), s["id"] + ".log",
        env={"ALLTILES": "1", "CLAIMTRACE": "1", "FP_OUT": fp, **node_env(s)}))
    j = json.load(open(p))
    return {"out": p, "tents_n": j["tents"]["n"], "tents_m2": j["tents"]["m2"], "overflow_polys": j["overflow"]["polys"]}


def t_fp_new(cfg, s, d):
    """polygons that newly ride a deck with the switch on (pe:diag/fp_new.py), in kept-claim (rk) format."""
    off, on = (os.path.splitext(d[k]["out"])[0] + "_fp.jsonl" for k in (s["census_off"], s["census_on"]))
    out = os.path.join(OUT, s["id"] + ".jsonl")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project("pe:diag/fp_new.py"), off, on, out], project("pe:"), s["id"] + ".log"))
    return {"out": p, "rows": sum(1 for l in open(p) if l.strip())}


def t_kept_lidar2(cfg, s, d):
    """the kept-claim LiDAR guard (pe:diag/lidar_kept_check2.py, DECKS from the round's terrain) on a step's rk list."""
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project("pe:diag/lidar_kept_check2.py"), ref(cfg, d, s["list"]), ref(cfg, d, s["drape"]),
                  out, project(s["base_drape"])], project("pe:"), s["id"] + ".log",
        env={"DECKS": os.path.join(project(s["terrain"]), "decks")}))
    j = json.load(open(p))
    return {"out": p, "kept": len(j["rows"]), "counts": json.dumps(j["counts"])}


def t_kept_lidar_dual(cfg, s, d):
    """the kept-claim LiDAR guard over two LiDAR class sets in one run (pe:reach/lidar_kept_dual.py); the gate judges
    set '1,2,17' (the lead's class-1 rule) and reports '17,2' beside it."""
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project("pe:reach/lidar_kept_dual.py"), ref(cfg, d, s["list"]), ref(cfg, d, s["drape"]),
                  out, project(s["base_drape"])], project("pe:"), s["id"] + ".log", env={"TERV": s["TERV"]}))
    j = json.load(open(p))
    judged = j["sets"]["1,2,17"]
    return {"out": p, "kept": len(judged["rows"]), "counts": json.dumps(judged["counts"])}


def t_lift_guards(cfg, s, d):
    """the page editor's lift guards (pe:guards/lift_guards.py): G1 vacate polygons and G2 step cells, variant vs base
    drape, only in tiles the variant wrote."""
    out = os.path.join(OUT, s["id"] + ".json")
    p = existing_or_run(cfg, s, out, lambda: run_job(
        s["id"], [py(), "-I", project("pe:guards/lift_guards.py"), ref(cfg, d, s["drape"]), project(s["base_drape"]),
                  project(s["LANES"]), out], project("pe:"), s["id"] + ".log"))
    t = json.load(open(p))["total"]
    return {"out": p, "tiles": t.get("tiles", 0), "vacate_polys": t.get("vacate_polys", 0),
            "step_cells": json.dumps(t.get("step_cells", {})), "defect_step_cells": t.get("defect_step_cells", 0)}


TOOLS = {"drape": t_drape, "lift_gate": t_lift_gate, "newlist": t_newlist, "deck_under": t_deck_under,
         "ramps": t_ramps, "trace": t_trace, "lidar": t_lidar, "census": t_census,
         "census_rs": lambda cfg, s, d: t_census(cfg, {**s, "rs_events": True}, d), "kept_lidar": t_kept_lidar, "tent_diff": t_tent_diff,
         "modelled_census": t_modelled_census, "census_fp": t_census_fp, "fp_new": t_fp_new, "kept_lidar2": t_kept_lidar2,
         "kept_lidar_dual": t_kept_lidar_dual, "lift_guards": t_lift_guards}


# ---------- gates (computed in code; budgets from the round file, default 0 = must not rise) ----------

def policy_debt(entry):
    """A standing policy (POLICY.md) adds a debt entry: recorded in this run's round.json (debt + changes), once."""
    rp = os.path.join(OUT, "round.json")
    r = json.load(open(rp))
    if not any(e.get("name") == entry["name"] for e in r.get("debt", [])):
        r.setdefault("debt", []).append(entry)
        r.setdefault("changes", []).append(datetime.date.today().isoformat() + ": " + entry["name"])
        if r.get("frames") is not None and entry.get("frame_pose"):   # the lead: every P2 debt site gets a frame
            r["frames"].setdefault("poses", []).append(entry["frame_pose"])
        json.dump(r, open(rp, "w"), indent=1, ensure_ascii=False)


def near(p, pts, r):
    return any(math.hypot(p["x"] - q["x"], p["z"] - q["z"]) <= q.get("radius_m", r) for q in pts)


def gate(cfg, g, d):
    b = cfg.get("budget", {})
    k = g["check"]
    if k == "newlist":
        n = d[g["step"]]
        j = json.load(open(n["out"]))
        new = [i for i in j["newList"] if i.get("c") in g.get("classes", ["step2"]) and i.get("mag", 0) >= g.get("min_m", 2)]
        debt = [i for i in new if near(i, [e for e in cfg.get("debt", []) if e.get("check", "newlist") == "newlist"
                                           and "x" in e], 30)]
        rest = [i for i in new if i not in debt]
        mx = b.get("marginal_step_max_m", 1e9)
        marginal = [i for i in rest if i["mag"] <= mx]
        unb = [i for i in rest if i["mag"] > mx]
        held = [t["name"] for t in cfg.get("target_fixed", []) if near(t, [dict(x, radius_m=15) for x in j["goneList"]], 15)]
        ok = not unb and len(marginal) <= b.get("new_step_places", 0)
        if g.get("targets_gate"):
            ok = ok and len(held) == len(cfg.get("target_fixed", []))
        what = "new places" if len(g.get("classes", ["step2"])) > 1 else f"new car steps >= {g.get('min_m', 2)} m"
        return ok, (f"{len(new)} {what}: {len(marginal)} small (<= {mx} m; budget "
                    f"{b.get('new_step_places', 0)}), {len(debt)} on the debt list, {len(unb)} bigger and not on it; "
                    f"target fixes held {len(held)} of {len(cfg.get('target_fixed', []))}"
                    + ("" if g.get("targets_gate") else " (reported)")), \
            {"unbudgeted": [f"{i.get('rname')} {i['mag']} m at {i['x']:.0f},{i['z']:.0f}" for i in unb][:30]}
    if k == "deck_under":
        v = d[g["step"]]
        base = d[g["base_step"]] if g.get("base_step") else {"underM": g["base_underM"]}
        lim = base["underM"] + b.get("deck_under_m", 0)
        zp = v.get("zero_piece_underM")
        ok = v["underM"] <= lim and zp in ("none", 0)
        zs = cfg.get("deck_zero_pieces")
        if zs:
            j = json.load(open(v["out"]))
            each = [(z["name"], zero_piece_m(j, z)) for z in zs]
            return ok, (f"underM {v['underM']} vs base {base['underM']} (limit {lim}); must show 0 m under: "
                        + ", ".join(f"{n} {m} m" for n, m in each)), {}
        return ok, f"underM {v['underM']} vs base {base['underM']} (limit {lim}); #6 piece {zp} m under (must be 0)", {}
    if k == "ramps":
        v = d[g["step"]]
        if g.get("base_step"):   # new flattened crossings vs the base drape's own list (the drive test's match)
            A = json.load(open(os.path.join(d[g["base_step"]]["out"], "FLATTENED.json")))["crossings"]
            B = json.load(open(os.path.join(v["out"], "FLATTENED.json")))["crossings"]
            has = lambda r, S: any(x["upper"] == r["upper"] and x["lower"] == r["lower"]
                                   and math.hypot(x["x"] - r["x"], x["z"] - r["z"]) <= 3 for x in S)
            new = [r for r in B if not has(r, A)]
            gone = [r for r in A if not has(r, B)]
            ok = len(new) <= b.get("ramps_new", 0)
            return ok, (f"flattened {len(B)} vs base {len(A)}: {len(new)} new, {len(gone)} gone "
                        f"(budget new {b.get('ramps_new', 0)})"), \
                {"unbudgeted": [f"{r['upper']} / {r['lower']} at {r['x']:.0f},{r['z']:.0f}" for r in new][:30]}
        ok = v["flattened"] <= v["previous"] + b.get("ramps_flattened", 0) and v["new"] <= b.get("ramps_new", 0)
        return ok, f"{v['line']} (budget flattened +{b.get('ramps_flattened', 0)}, new {b.get('ramps_new', 0)})", {}
    if k == "modelled_split":   # the coordinator's rule: with the switch on, no line is modelled only by inheritance
        on, off = d[g["step"]], d[g["base_step"]]
        rides = d.get("p_fp_new", {}).get("rows", "?")
        if g.get("scope") == "city_street":   # the lead's term (a): only city-street-class lines are gated
            if int(on.get("inherited_street_lines", -1)) < 0:
                return False, ("switch on: the census doesn't class its inherited lines (no city_street per row): "
                               "can't judge the city-street scope, fails closed"), {}
            # optional, from the round file: named city lines that may stay inherited, caps, lines that must be freed
            J = json.load(open(on["out"]))
            cityrows = [r for r in J.get("rows", []) if r.get("cls") == "inherited"
                        and (r["city_street"] if "city_street" in r else CITY_HW.match(r.get("hw") or ""))]
            allow = {str(o) for o in g.get("allowed_city_osm", [])}
            stray = [r for r in cityrows if not allow & {str(o) for o in r.get("osm", [])}]
            freed = {str(o) for r in J.get("freed_rows", []) for o in r.get("osm", [])}
            missing = [str(o) for o in g.get("must_free_osm", []) if str(o) not in freed]
            cm = lambda rs: round(sum(r.get("len", 0) for r in rs))
            caps = [(len(cityrows), g.get("max_city_lines", 0), "city lines"), (cm(cityrows), g.get("max_city_m", 0), "city m"),
                    (int(on["inherited_lines"]), g.get("max_total_lines", 1e9), "inherited lines"),
                    (float(on["inherited_m"]), g.get("max_total_m", 1e9), "inherited m")]
            over = [f"{lbl} {v} > {c}" for v, c, lbl in caps if v > c]
            ok = not stray and not missing and not over
            return ok, (f"switch on: {len(cityrows)} city-street-class lines / {cm(cityrows)} m inherit 'modelled' "
                        f"(allowed: {len(allow)} named lines, caps {g.get('max_city_lines', 0)} lines / "
                        f"{g.get('max_city_m', 0)} m); {len(stray)} not on the allowed list; must be freed: "
                        f"{len(g.get('must_free_osm', []))}, missing {missing or 'none'}; over a cap: {over or 'none'}; "
                        f"{on['inherited_other_lines']} motorway / trunk / ext lines / "
                        f"{on['inherited_other_m']} m still inherit (reported, not gated: the lead's scope); switch off: "
                        f"{off['inherited_lines']} lines / {off['inherited_m']} m; polygons newly riding with the switch on: "
                        f"{rides}"), {"unbudgeted": [f"{r.get('name')} (osm {','.join(map(str, r.get('osm', [])))}, "
                                         f"{r.get('hw')}, {r.get('len')} m)" for r in stray][:40]}
        ok = int(on["inherited_lines"]) == 0
        return ok, (f"switch on: {on['inherited_lines']} lines / {on['inherited_m']} m inherit 'modelled' (must be 0); "
                    f"switch off (the lead's required count): {off['inherited_lines']} deck lines / {off['inherited_m']} m "
                    f"excluded only through a lane-hugging neighbour, of them by R2 lines {off['inherited_by_r2']}; "
                    f"polygons newly riding with the switch on: {rides}"), {}
    if k in ("frames_capture", "frames_structure"):   # G0 / G3, stopping checks before Hydrogen
        f = os.path.join(project(cfg["frames"]["folder"]), "guards_frames.json")
        if not os.path.exists(f):
            return False, f"no guards_frames.json in {cfg['frames']['folder']} (the page editor writes it with the frames): fails closed", {}
        pairs = json.load(open(f)).get("pairs", [])
        lim = g.get("max_diff_pct", 0.05)
        if k == "frames_capture":   # A vs B pixel difference on tiles the variant did not write
            v = lambda x: x.get("g0_pixel_diff_pct", x.get("capture_diff_pct"))
            bad = [x for x in pairs if v(x) is None or v(x) >= lim]
            return bool(pairs) and not bad, (f"{len(pairs)} pairs; {len(bad)} with a capture difference >= {lim} % on "
                                             f"unwritten tiles (must be 0: re-shoot before Hydrogen)"), \
                {"unbudgeted": [f"{x.get('pose')}: {v(x)} %" for x in bad][:40]}
        lost = lambda x: x.get("g3_lost", x.get("structure_lost"))
        bad = [x for x in pairs if lost(x) is None or lost(x) > 0]
        return bool(pairs) and not bad, (f"{len(pairs)} poses; {len(bad)} lose structure pieces A vs B (must be 0); "
                                         f"triangles / instances per pose in items"), \
            {"unbudgeted": [f"{x.get('pose')}: {lost(x)} pieces lost" for x in bad][:40],
             "listed": [f"{x.get('pose')}: tris {x.get('g3_tris_a', x.get('tris_a'))} -> {x.get('g3_tris_b', x.get('tris_b'))}, "
                        f"instances {x.get('g3_inst_a', x.get('inst_a'))} -> {x.get('g3_inst_b', x.get('inst_b'))}"
                        for x in pairs][:40]}
    if k in ("hydrogen_pose", "hydrogen_none_worse"):   # read Hydrogen's per-site rows (hydrogen_frames.json)
        f = os.path.join(OUT, "hydrogen_frames.json")
        rows = json.load(open(f)) if os.path.exists(f) else []
        if not rows:
            return False, "no Hydrogen rows: fails closed", {}
        if k == "hydrogen_pose":
            mine = [r for r in rows if g["pose"] in r["site"]]
            if not mine:
                return False, f"no frame for pose {g['pose']} was reviewed: fails closed", {}
            ok = all(r.get("answer") == "yes" for r in mine)
            return ok, (f"{g['pose']}: Hydrogen answers {', '.join(repr(r.get('answer')) for r in mine)} to "
                        f"\"{g['question']}\" (must be yes): {'; '.join(r.get('answer_why', '') for r in mine)}"), \
                {"unbudgeted": [] if ok else [f"{r['site']}: {r.get('answer')}: {r.get('answer_why', '')}" for r in mine]}
        stop = ("worse",) + (() if g.get("allow_mixed") else ("mixed",))
        bad = [r for r in rows if r.get("verdict") in stop or not r.get("verdict")]
        by = {}
        for r in rows: by[r.get("verdict") or "none"] = by.get(r.get("verdict") or "none", 0) + 1
        return not bad, (f"{len(rows)} poses reviewed: {json.dumps(by)}; {len(bad)} judged "
                         f"{'/'.join(stop)} or unjudged (must be 0)"), \
            {"unbudgeted": [f"{r['site']}: {r.get('verdict') or 'no verdict'}: new {'; '.join(r.get('new', []))[:200]}"
                            for r in bad][:40]}
    if k == "lift_vacate":   # G1: no raise may leave a real street (LiDAR at the base height) undrawn
        jg = json.load(open(d[g["step"]]["out"]))
        dl = [e for e in cfg.get("debt", []) if e.get("check") == "lift_vacate"]
        at = lambda v: {"x": v["c"][0], "z": v["c"][1]}
        bad = [v for v in jg.get("vacate", []) if not near(at(v), [dict(e, radius_m=5) for e in dl], 5)]
        row = lambda v: (f"vacate {v['tile'].replace('.drape.json', '')} {v['poly'][0]} {v['poly'][1]} at {v['c'][0]},"
                         f"{v['c'][1]} (raised {v.get('raised')}/{v.get('of')} vertices up to {v.get('dmax')} m; base "
                         f"height on LiDAR at {v.get('base_on_lidar')}/{v.get('sampled')})")
        return not bad, (f"{len(jg.get('vacate', []))} vacate polygons in {jg['total'].get('tiles', 0)} written tiles; "
                         f"{len(jg.get('vacate', [])) - len(bad)} on the lead's debt list"
                         + (f" ({sum(1 for e in dl if e.get('conditional'))} conditional: they hold only while their "
                            f"condition gate passes)" if any(e.get("conditional") for e in dl) else "")
                         + f"; {len(bad)} not (must be 0)"), \
            {"unbudgeted": [row(v) for v in bad][:40], "listed": [row(v) for v in jg.get("vacate", [])][:40]}
    if k == "lift_step":   # G2: low-only steps none; high-only within budget and only in the lead's allowed tiles
        jg = json.load(open(d[g["step"]]["out"]))
        allowed = set(g.get("high_only_tiles", []))
        tid = lambda t: t.replace(".drape.json", "").replace("tile_", "")
        steps = jg.get("steps", [])
        low = [x for x in steps if x["cls"] == "low-only"]
        high = [x for x in steps if x["cls"] == "high-only"]
        high_out = [x for x in high if tid(x["tile"]) not in {tid(a) for a in allowed}]
        lim_lo, lim_hi = b.get("step_low_only", 0), b.get("step_high_only", 0)
        ok = len(low) <= lim_lo and len(high) <= lim_hi and not high_out
        per = {}
        for x in steps:
            per.setdefault(tid(x["tile"]), {}).setdefault(x["cls"], 0)
            per[tid(x["tile"])][x["cls"]] += 1
        two = jg["total"].get("step_cells", {}).get("two-level", 0)
        return ok, (f"low-only {len(low)} (budget {lim_lo}); high-only {len(high)} (budget {lim_hi}, allowed tiles "
                    f"{', '.join(sorted(tid(a) for a in allowed)) or 'none'}; {len(high_out)} outside them); two-level "
                    f"{two} (allowed); none {jg['total'].get('step_cells', {}).get('none', 0)} (reported); per tile "
                    f"{json.dumps(per)}"), \
            {"unbudgeted": [f"{x['cls']} step {x['tile'].replace('.drape.json', '')} at {x['x']},{x['z']} ({x['lo']} -> "
                            f"{x['hi']} m)" for x in low + high_out][:40]}
    if k == "kept_lidar":   # every kept claim the rule raised: on LiDAR (PASS), or a FAIL with a reason on file
        jk = json.load(open(d[g["step"]]["out"]))
        dual = "sets" in jk   # a kept_lidar_dual step: judge the '1,2,17' set (the lead's class-1 rule)
        rows = jk["sets"][g.get("set", "1,2,17")]["rows"] if dual else jk["rows"]
        rs = json.load(open(project(cfg["kept_reasons"]))) if cfg.get("kept_reasons") and os.path.exists(project(cfg["kept_reasons"])) else []
        dl = [e for e in cfg.get("debt", []) if e.get("check") == "kept_lidar"]   # sites the lead put on the debt list
        rs = rs + dl
        at = lambda r: {"x": r["c"][0], "z": r["c"][1]}
        fails = [r for r in rows if r.get("cls") == "FAIL"]
        bad = [r for r in fails if not near(at(r), [dict(q, radius_m=5) for q in rs], 5)]
        # the lead: slivers are defects. A sliver point the variant draws > 2 m above the base fails the round unless
        # it has a reason. Needs each row's `slivers` list [[x, z, h_variant, h_base], ...]; a sliver count with no
        # list can't be checked, so it fails closed.
        # the lead (revised): a spike (> 2 m above the base, or the base draws nothing) FAILS only if no continuous
        # LiDAR layer lies within 1 m of its drawn height. Needs each entry's 5th element: the continuous LiDAR layer
        # heights at the point. Without it the spike can't be checked: fails closed. Spikes on LiDAR are reported.
        spikes, unlisted, on_lidar = [], [], []
        for r in rows:
            if r.get("sliver") and "slivers" not in r:
                unlisted.append(r)
            for e in r.get("slivers") or []:
                x, z, h, hb = e[:4]
                if not (hb is None or h - hb > 2):
                    continue
                cont = e[4] if len(e) > 4 else None
                on = e[5] if len(e) > 5 else (any(abs(c - h) <= 1.0 for c in cont) if cont is not None else None)
                if on:   # the page editor's on_lidar flag: a continuous layer within 1 m of the drawn height
                    on_lidar.append((r, x, z, h, hb))
                elif not near({"x": x, "z": z}, [dict(q, radius_m=5) for q in rs], 5):
                    spikes.append((r, x, z, h, hb, cont))
        by = {}
        for r in rows: by[r.get("cls", "?")] = by.get(r.get("cls", "?"), 0) + 1
        # the lead (option c): a PASS on only 1-2 raised points counts as D for the cap; a 1-2-point FAIL stays a FAIL
        small_pass = sum(1 for r in rows if r.get("small") and r.get("cls") == "PASS")
        by_cap = dict(by, PASS=by.get("PASS", 0) - small_pass, D=by.get("D", 0) + small_pass)
        raised = by_cap.get("PASS", 0) + by_cap.get("FAIL", 0) + by_cap.get("D", 0)   # NOLIFT doesn't count
        d_share = by_cap.get("D", 0) / raised if raised else 0.0
        unsure = d_share > 0.2   # the lead: too much of the rule unverified -> uncertain, blocks
        return (not bad and not unsure and not spikes and not unlisted), (("UNCERTAIN (blocks): " if unsure else "")
                + (f"{len(spikes)} sliver spikes > 2 m off LiDAR (or LiDAR not logged) without a reason; " if spikes else "")
                + (f"{len(on_lidar)} sliver spikes on LiDAR (reported, not failed); " if on_lidar else "")
                + (f"{len(unlisted)} claims with slivers but no sliver locations logged (fails closed); " if unlisted else "")
                + f"{len(rows)} kept claims: {by}; {len(fails)} FAIL, {len(fails) - len(bad)} of them with a reason; "
                + (f"{len(dl)} sites on the lead's debt list (counted as reasoned); " if dl else "")
                + f"D for the cap {by_cap.get('D', 0)} of {raised} that raised anything = {d_share:.0%} (cap 20 %; "
                f"includes {small_pass} PASS on only 1-2 points); verdicts on only 1-2 raised points: "
                f"{sum(1 for r in rows if r.get('small'))} ({small_pass} PASS, "
                f"{sum(1 for r in rows if r.get('small') and r.get('cls') == 'FAIL')} FAIL); sliver points (raised "
                f"alone, not counted as evidence): {sum(int(r.get('sliver') or 0) for r in rows)} on "
                f"{sum(1 for r in rows if r.get('sliver'))} claims"
                + (f"; set 17,2 for comparison: {jk.get('counts_17_2')}; PASS on class-1 LiDAR only: "
                   f"{jk.get('pass_c1_only')}" if dual else "")), \
            {"unbudgeted": ([f"FAIL at {r['c'][0]:.0f},{r['c'][1]:.0f}: {r.get('match')}/{r.get('n')} raised points on "
                             f"LiDAR" for r in bad]
                            + [(f"sliver spike {h - hb:.1f} m" if hb is not None else "sliver where the base draws nothing")
                               + f" at {x:.1f},{z:.1f} (claim at {r['c'][0]:.0f},{r['c'][1]:.0f}; variant {h:.1f} m, base "
                               + (f"{hb:.1f} m" if hb is not None else "none")
                               + (f"; LiDAR layers {cont})" if cont is not None else "; LiDAR not logged, fails closed)")
                               for r, x, z, h, hb, cont in spikes])[:40]}
    if k == "tent_diff":   # new tents anywhere the variant wrote a tile
        j = json.load(open(d[g["step"]]["out"]))
        t, lim = j["total"], b.get("tents_new", 0)
        new = [(r["tile"], w) for r in j["rows"] for w in r.get("new", [])]
        # POLICY 2: small growth at existing tent sites (<= 2 tents, <= 1 m², only in tiles that had tents) -> debt
        if t["n_new"] > lim and t["n_new"] - lim <= 2 and t["m2_new"] <= 1.0 + 1e-6 and all(
                r["n_base"] > 0 for r in j["rows"] if r["n_var"] > r["n_base"]):
            sites = [r["tile"] for r in j["rows"] if r["n_var"] > r["n_base"]]
            policy_debt({"name": f"policy 2: tents grow at existing sites {sites} (+{t['n_new']}, +{t['m2_new']} m²)",
                         "tents_n": t["n_new"], "tents_m2": t["m2_new"], "owner": "page: tents class",
                         "pay_back": "with the tents class fix", "added_by": "policy 2 (POLICY.md)",
                         "frame_pose": "policy 2 tent site(s): " + "; ".join(
                             f"{w[0]},{w[1]} ({tile})" for tile, w in new[:4])})
            return True, (f"tents {t['n_var']} vs base {t['n_base']}: {t['n_new']} new, {t['m2_new']} m², all at existing "
                          f"tent sites {sites}: on the debt list by policy 2"), {"unbudgeted": [f"tent {w[2]} m tall, {w[3]} m² at {w[0]},{w[1]} ({tile})" for tile, w in new][:40]}
        return t["n_new"] <= lim, (f"tents {t['n_var']} vs base {t['n_base']}: {t['n_new']} new, {t['m2_new']} m² "
                                   f"(limit +{lim}); {t.get('tiles_changed', '?')} of {t.get('tiles_written', '?')} written "
                                   f"tiles differ"), \
            {"unbudgeted": [f"tent {w[2]} m tall, {w[3]} m² at {w[0]},{w[1]} ({tile})" for tile, w in new][:40]}
    if k == "census":
        v, base = d[g["step"]], d[g["base_step"]]
        dn = sum(e.get("tents_n", 0) for e in cfg.get("debt", []))      # tents the lead put on the debt list
        dm = sum(e.get("tents_m2", 0) for e in cfg.get("debt", []))
        ok = (v["tents_n"] - dn <= base["tents_n"] + b.get("tents_n", 0)
              and v["tents_m2"] - dm <= base["tents_m2"] + b.get("tents_m2", 0) + 1e-6)
        return ok, (f"tents {v['tents_n']} / {v['tents_m2']} m² vs base {base['tents_n']} / {base['tents_m2']} m²"
                    + (f"; {dn} tents / {dm} m² on the debt list" if dn or dm else "")), {}
    if k == "lift":
        v, base = d[g["step"]], d[g["base_step"]]
        rows = [(f, v[f], base[f], b.get(f, 0)) for f in g.get("fields", ("visible_drivable_new", "lost_contact_flags"))]
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
                seen = [q for l in sites.values() for q in l]
                if len(lst) < f.get("per_class", 4) and not near(r, seen, 5):   # same site in two lists: once
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


PAGE_ASK = ("These are frames from a 3D city driving game, all from the same camera view: either one image with the "
            "panels side by side, or one image per panel. The panels, in order (left to right), are: {panels}. In each panel list the visible rendering defects (missing "
            "road, floating or misplaced geometry, steps or drops in the road surface, walls or slabs where they "
            "should not be, gaps, see-through holes, z-fighting). Say which defects of the FIRST panel are fixed in "
            "the later panels and which are new there, and where in the frame. End with exactly one JSON block:\n"
            "```json\n{{\"fixed_vs_first\": [\"...\"], \"new_vs_first\": [\"...\"], "
            "\"verdict\": \"better|same|worse|mixed\"}}\n```")


ROWS_MAX = 6000   # the step output's "rows" stays valid JSON: past this, a short form; the full rows are in the file


def hydrogen_frames(cfg, expect=None):
    """One Hydrogen review per site. A site is one side-by-side image, or several files named <site>__<n>.<ext>
    (n = panel order), which are reviewed together. expect: the request's site names; a folder whose sites differ
    is refused before any review (the lead, 2026-11-22)."""
    from qa import hydrogen_review
    folder = frames_folder(cfg)
    names = cfg["frames"].get("panels") or ["before", "after"]
    sites = {}
    for n in sorted(x for x in os.listdir(folder) if x.lower().endswith(IMG)):
        sites.setdefault(os.path.splitext(n)[0].split("__")[0], []).append(n)
    if expect is not None and set(sites) != set(expect):
        extra, missing = sorted(set(sites) - set(expect)), sorted(set(expect) - set(sites))
        raise RuntimeError("frames folder sites don't match the request's poses: "
                           + (f"not requested {extra[:8]}" if extra else "") + ("; " if extra and missing else "")
                           + (f"missing {missing[:8]}" if missing else "") + " (fix the folder, then re-drop READY)")
    import time
    from common import Running
    t0, rows, waiting = time.time(), [], 0
    jobs = os.path.join(OUT, "jobs")
    state = lambda tag: ("done" if os.path.exists(os.path.join(jobs, "hydrogen_" + tag, "rc")) else
                         "running" if os.path.exists(os.path.join(jobs, "hydrogen_" + tag, "job.json")) else "new")
    import hashlib
    def ftag(site):   # keyed by content: a re-shot frame gets a fresh review, an unchanged one keeps its verdict
        h = hashlib.sha256(b"".join(open(os.path.join(folder, f), "rb").read() for f in sites[site])).hexdigest()[:10]
        return "page_" + re.sub(r"[^A-Za-z0-9_.-]", "_", site) + "_" + h
    # a pose's own yes/no question (a hydrogen_pose gate: the lead's condition at that pose) rides on its review
    qs = {site: [g for g in cfg.get("gates", []) if g.get("check") == "hydrogen_pose" and g.get("pose") and g["pose"] in site]
          for site in sites}
    tags = {site: ftag(site) + ("_q" + hashlib.sha256("".join(g["question"] for g in qs[site]).encode()).hexdigest()[:6]
                                if qs[site] else "") for site in sites}
    for site, files in sites.items():   # at most HYD_PARALLEL Hydrogen calls at once; never block past the deadline
        tag = tags[site]
        st = state(tag)
        if st != "done":
            running = sum(state(t) == "running" for t in tags.values())
            if st == "new" and running >= HYD_PARALLEL:
                waiting += 1
                continue
        if len(files) == 1:
            where, panels = "", ", ".join(names)
        else:
            where = "The images are the panels in this order: " + "; ".join(
                f"{f} = {names[i] if i < len(names) else 'panel ' + str(i + 1)}" for i, f in enumerate(files)) + "."
            panels = ", ".join(names[:len(files)])
        try:
            ask = PAGE_ASK.format(panels=panels)
            if qs[site]:
                ask += ("\n\nAlso answer, about the LAST panel only: " + " / ".join(g["question"] for g in qs[site])
                        + " Add to the same JSON block \"answer\": \"yes\" or \"no\", and \"answer_why\": one sentence.")
            v = hydrogen_review(tag, [(f, os.path.join(folder, f)) for f in files], where,
                                ask=ask, wait=0)   # start or collect; the driver comes back
        except Running:
            waiting += 1
            continue
        rows.append({"site": site, "files": files, "verdict": v.get("verdict", ""), "fixed": v.get("fixed_vs_first", []),
                     "new": v.get("new_vs_first", []), "seconds": v["seconds"],
                     **({"question": " / ".join(g["question"] for g in qs[site]), "answer": str(v.get("answer", "")).lower(),
                         "answer_why": v.get("answer_why", "")} if qs[site] else {})})
    if waiting:
        raise Running(f"job running: Hydrogen, {len(rows)} of {len(sites)} sites reviewed")
    json.dump(rows, open(os.path.join(OUT, "hydrogen_frames.json"), "w"), indent=1)
    by = {}
    for r in rows:
        by[r["verdict"] or "none"] = by.get(r["verdict"] or "none", 0) + 1
    return {"frames": len(rows), "verdicts": json.dumps(by), "rows": rows_field(rows), "rows_file": "hydrogen_frames.json"}


def rows_field(rows):
    """Valid JSON always (r14x2-1: a cut [:6000] broke every reader): the full rows when short, else site + verdict."""
    full = json.dumps(rows)
    if len(full) <= ROWS_MAX:
        return full
    short = [{"site": r.get("site"), "verdict": r.get("verdict"), **({"override": r["override"]} if r.get("override") else {})}
             for r in rows]
    return json.dumps(short)


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


def trace_tables(cfg):
    """Per trace class: sites and step metres; and trace class x LiDAR class, joined on site position (0.5 m).
    Built from the round's own trace and LiDAR outputs, for every trace step that has a LiDAR step on the same list."""
    out = {}
    tr = [s for s in cfg["steps"] if s["tool"] == "trace"]
    li = [s for s in cfg["steps"] if s["tool"] == "lidar"]
    for t in tr:
        try:
            rows = json.load(open(step_out(cfg, t["id"])))["rows"]
        except (OSError, ValueError, KeyError):
            continue
        per = {}
        for r in rows:
            c = per.setdefault(r["cls"], {"sites": 0, "step_m": 0.0, "max_m": 0.0})
            c["sites"] += 1; c["step_m"] = round(c["step_m"] + r.get("mag", 0), 2); c["max_m"] = max(c["max_m"], r.get("mag", 0))
        o = {"per_class": per, "total_step_m": round(sum(r.get("mag", 0) for r in rows), 2)}
        for l in li:
            if l["list"] != t["list"]:
                continue
            try:
                lrows = json.load(open(step_out(cfg, l["id"])))["rows"]
            except (OSError, ValueError, KeyError):
                continue
            def lcls(r):
                c = r.get("cls", "?")
                return ("C-none" if not r.get("lidar_cont") else "C-other") if c == "C" else c
            x = {}
            for r in rows:
                m = next((q for q in lrows if abs(q["x"] - r["x"]) <= 0.5 and abs(q["z"] - r["z"]) <= 0.5), None)
                k = lcls(m) if m else "unmatched"
                cell = x.setdefault(r["cls"], {}).setdefault(k, {"sites": 0, "step_m": 0.0})
                cell["sites"] += 1; cell["step_m"] = round(cell["step_m"] + r.get("mag", 0), 2)
            o["trace_x_lidar"] = x
            o["lidar_legend"] = ("A: LiDAR shows a continuous road at both the high and the low drawn side; B: only at the "
                                 "high (restored) side; C-other: a continuous LiDAR road at some other height; C-none: no "
                                 "continuous LiDAR road there; D: no LiDAR, no deck line within 15 m, or no drawn surface")
        out[t["id"]] = o
    return out


def page_accept(req, cfg, d):
    gates = {k: v for k, v in d.items() if k.startswith("g_")}
    rules_pass = all(v.get("pass") == "true" for v in gates.values())
    expired = bool(cfg.get("expires")) and datetime.date.today() > datetime.date.fromisoformat(cfg["expires"])
    rules_pass = rules_pass and not expired
    brief = {k: cfg[k] for k in ("idea", "question", "success_measure", "budget", "debt", "target_fixed", "expires")
             if k in cfg}
    if (cfg.get("frames") or {}).get("unreadable_note"):
        brief["frames_note"] = cfg["frames"]["unreadable_note"]
    ev = {k: {kk: vv for kk, vv in v.items() if kk not in ("out", "rows")} for k, v in d.items()}   # no local paths
    tables = trace_tables(cfg)
    if tables:
        ev["trace_tables"] = tables
        json.dump(tables, open(os.path.join(OUT, "trace_tables.json"), "w"), indent=1)
    hf = os.path.join(OUT, "hydrogen_frames.json")
    if os.path.exists(hf):   # every frame's verdict in full, compactly (the step output itself is capped)
        ev["frame_reviews"] = [{"site": r["site"], "verdict": r["verdict"],
                                "fixed": [x[:160] for x in r["fixed"][:3]], "new": [x[:160] for x in r["new"][:3]]}
                               for r in json.load(open(hf))]
    prompt = (open(os.path.join(os.path.dirname(__file__), "..", "prompts", f"page_accept_{cfg['accept']}.md")).read()
              + "\n\nRound: " + cfg["round"] + "\n\nThe round as declared up front:\n" + json.dumps(brief, indent=1)
              + f"\n\nGate verdict, computed in code: {'PASS' if rules_pass else 'FAIL'}"
              + (" (past its expiry date)" if expired else "")
              + "\n\nEvidence:\n" + json.dumps(ev, indent=1)[:40000])
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
