#!/usr/bin/env python3
"""Combined-layer check for terrain rounds (the lead, 2026-11-22): this round's terrain against the previous frozen terrain,
both under the same pinned highway base (lane_guard's lanes), with macmini's census scripts (highways/work/census/r6u):
  terrain_ab_check.py   DRIVE: rays from a driver's eye that see through in B but hit something in A  -> new driver-height cells
                        AIR:   rays down at cells near a lane that reach the void in B but not in A    -> new aerial void cells
  lost_surface.py       ground more than 1 m lower in B with no highway surface to carry it              -> lost-surface m2
A round opts in with a "combined_layer" block:
  {"hwy": "highways/work/p2net/final_fdr/<base>",   default: lane_guard's lanes folder (the pinned highway base, r14t)
   "prev": "<previous frozen terrain folder>",       default: lane_guard's prev, else the round's prev (TERV_A)
   "tiles": "<project path to {"tiles": [...]}>",     e.g. work/terrain_study/p2/checks/<round>_lane_cut.json
   "scripts_sha": {"terrain_ab_check.py": "<sha256 prefix>", "lost_surface.py": "...", "street_carry.py": "...",
                   "g0shim.py": "..."},              every file pinned is re-checked before each step; g0shim.py is required
   "street_carry": {"osm_ids": [...]},              optional, report-only (the lead's E 70 St rule)
   "reasons": "<project path>"}                       written only AFTER the stop, from <run>/combined_layer.json
TERV_B is the round itself (its folder under work/terrain_study/p1/terrain/). The scripts run with FULL=1.
Steps (after lg_manifest, so the highway base is the pinned one):
  cl_ab     job: terrain_ab_check.py HWY PREV ROUND TILES OUT
  cl_lost   job: lost_surface.py     HWY PREV ROUND TILES OUT
  cl_street job, report-only (when asked): street_carry.py HWY PREV ROUND OUT OSM_ID...
  cl_gate   stop: any new driver-height cell, new aerial void cell or lost-surface cell without a reason. Never passes when a
            key list doesn't account for its count. Writes <run>/combined_layer.json: every cell, its key and its reason.
Reasons file: {"drive": {key: why}, "air": {key: why}, "lost": {key: why}}, one entry per 2 m cell, keyed as the scripts write:
  drive  "<tile>,<floor(x/2)>,<floor(z/2)>,<side>"   air / lost  "<tile>,<floor(x/2)>,<floor(z/2)>"   (tile like x10_z-18)
Each script is pinned by its sha256 prefix in the round file; a mismatch stops the step before it runs.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os

from common import OUT, deps, load_request, project, respond, round_config, run_job, run_main, sha256, validate

CENSUS = "highways/work/census/r6u"
OPENED = ("void", "terrain_below", "other")   # 'road' (a deck > 0.3 m off the lane) is listed by the script, not a hole


def cl(cfg):
    g = dict(cfg["combined_layer"])
    lg = cfg.get("lane_guard") or {}
    g.setdefault("hwy", lg.get("lanes"))
    g.setdefault("prev", lg.get("prev") or cfg["prev"])
    if not g.get("hwy") or not g.get("tiles"):
        raise RuntimeError("combined_layer needs 'hwy' (or a lane_guard block) and 'tiles'")
    return g


def py():
    return project(".venv/bin/python")


def job(name, script, args, env=None):
    rc = run_job(name, [py(), "-I", project(f"{CENSUS}/{script}")] + args, project(CENSUS), f"{name}.log",
                 env={**(env or {}), "FULL": "1"})   # FULL=1: every row uncapped (the key lists are complete either way)
    if rc != 0:
        raise RuntimeError(f"{name}: {script} exited {rc} (see {name}.log)")


SHARED = ("g0shim.py",)   # macmini's shared frame helper (CBE tiles have no _REF): every census script imports it


def sha_check(g, script):
    """The round pins macmini's scripts by sha256 prefix (combined_layer.scripts_sha). Before a step runs, every pinned file
    is re-checked, and the step's script plus the shared helpers it imports must be pinned; a mismatch stops the step."""
    pins = dict(g.get("scripts_sha") or {})
    need = [script] + [f for f in SHARED if os.path.exists(project(f"{CENSUS}/{f}"))]
    missing = [f for f in need if not pins.get(f)]
    if missing:
        raise RuntimeError(f"combined_layer.scripts_sha has no pin for {', '.join(missing)}")
    bad = []
    for f, want in sorted(pins.items()):
        got = sha256(project(f"{CENSUS}/{f}")) if os.path.exists(project(f"{CENSUS}/{f}")) else "missing"
        if not got.startswith(want):
            bad.append(f"{f} is {got[:12]}, pinned {want}")
    if bad:
        raise RuntimeError("not the check this round declares: " + "; ".join(bad))


def evaluate(ab, ls, reasons):
    """-> (ok, summary dict, rows dict). Pure: the gate and the tests use it. Reasons are by the scripts' own 2 m cell keys,
    which the scripts always write in full (new_drive_cell_keys / new_air_cell_keys / lost_cell_keys)."""
    rs = {k: dict(reasons.get(k) or {}) for k in ("drive", "air", "lost")}
    keys = {"drive": set(ab.get("new_drive_cell_keys") or []), "air": set(ab.get("new_air_cell_keys") or []),
            "lost": set(ls.get("lost_cell_keys") or [])}
    rows = {k: {key: {"key": key, "reason": rs[k].get(key)} for key in sorted(keys[k])} for k in keys}
    for r in ab.get("new_drive", []):
        d = rows["drive"].get(r.get("key"))
        if d is not None:
            d.setdefault("rays", 0); d["rays"] += 1
            d.setdefault("classes", set()).add(r["class"]); d.setdefault("ways", set()).add(r["way"])
            d.setdefault("x", r["x"]); d.setdefault("z", r["z"])
    for r in ab.get("new_air", []):
        a = rows["air"].get(r.get("key"))
        if a is not None:
            a.setdefault("rays", 0); a["rays"] += 1; a.setdefault("x", r["x"]); a.setdefault("z", r["z"])
    for r in ls.get("lost_cells", []):
        c = rows["lost"].get(r.get("key"))
        if c is not None:
            c.update({k: r[k] for k in ("x", "z", "a", "b") if k in r})
    for d in rows["drive"].values():
        for k in ("classes", "ways"):
            if k in d: d[k] = sorted(d[k])
    # completeness: the key lists must account for every counted cell (4 m2 per lost cell)
    incomplete = []
    if "new_drive_cell_keys" not in ab or "new_air_cell_keys" not in ab or "lost_cell_keys" not in ls:
        incomplete.append("a script output has no key list (old script version?)")
    if len(keys["drive"]) != int(ab.get("new_drive_hole_cells", 0)):
        incomplete.append(f"drive keys {len(keys['drive'])} != new_drive_hole_cells {ab.get('new_drive_hole_cells')}")
    if len(keys["air"]) != int(ab.get("new_air_void_cells", 0)):
        incomplete.append(f"air keys {len(keys['air'])} != new_air_void_cells {ab.get('new_air_void_cells')}")
    if 4 * len(keys["lost"]) != int(ls.get("lost_m2", 0)):
        incomplete.append(f"lost keys {len(keys['lost'])} x 4 m2 != lost_m2 {ls.get('lost_m2')}")
    un = {k: [r for r in rows[k].values() if not r["reason"]] for k in rows}
    unknown = {k: sorted(set(rs[k]) - keys[k]) for k in rs}
    s = {"drive_cells": len(keys["drive"]), "air_cells": len(keys["air"]), "lost_m2": int(ls.get("lost_m2", 0)),
         "unreasoned_drive_cells": len(un["drive"]), "unreasoned_air_cells": len(un["air"]),
         "unreasoned_lost_m2": 4 * len(un["lost"]), "incomplete": incomplete,
         "reasons_for_cells_not_in_output": {k: len(v) for k, v in unknown.items()},
         "drive_opened": {c: int(ab.get(f"drive_opened_{c}", 0)) for c in OPENED + ("road",)},
         "cut_to_road_m2": ls.get("cut_to_road_m2"), "carried_m2": ls.get("carried_m2"),
         "under_deck_m2": ls.get("under_deck_m2"), "drive_backed": ab.get("drive_backed"),   # reported, not gated (the lead)
         "full_rows": bool(ab.get("full")) and bool(ls.get("full"))}
    ok = not incomplete and not any(un.values())
    # under-deck ground (B cut down below a deck): not gated; the lead judges it with frames, so it goes to the lead-gate summary
    ud = [{k: c.get(k) for k in ("tile", "x", "z", "m2", "a", "b", "deck_y_min", "deck_over_b_min", "deck_mat")}
          for c in ls.get("under_deck_clusters") or []]
    s["under_deck_clusters"] = len(ud)
    if int(ls.get("under_deck_m2") or 0) and sum(int(c["m2"] or 0) for c in ud) < int(ls["under_deck_m2"]):
        s["incomplete"].append(f"under-deck clusters list {sum(int(c['m2'] or 0) for c in ud)} of {ls['under_deck_m2']} m2")
        ok = False
    return ok, s, {**{k: list(v.values()) for k, v in rows.items()}, "under_deck_clusters": ud,
                   "reasons_for_cells_not_in_output": unknown}


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        return validate(req, c.get("pass", "true") == "true")
    cfg = round_config()
    g = cl(cfg)
    r, prev = cfg["round"], g["prev"]
    base = [project(g["hwy"]), prev, r, project(g["tiles"])]
    folder = os.path.join(OUT, "combined")
    os.makedirs(folder, exist_ok=True)

    if tid == "cl_ab":
        out = os.path.join(folder, "drive_air.json")
        if not os.path.exists(out):
            sha_check(g, "terrain_ab_check.py")
            job(tid, "terrain_ab_check.py", base + [out], g.get("env"))
        s = json.load(open(out))
        return respond(req, output={"out": out, "new_drive_hole_cells": s["new_drive_hole_cells"],
                                    "new_air_void_cells": s["new_air_void_cells"]})
    if tid == "cl_lost":
        out = os.path.join(folder, "lost_surface.json")
        if not os.path.exists(out):
            sha_check(g, "lost_surface.py")
            job(tid, "lost_surface.py", base + [out], g.get("env"))
        s = json.load(open(out))
        return respond(req, output={"out": out, "lost_m2": s["lost_m2"]})
    if tid == "cl_street":   # report-only (the lead's E 70 St rule): runs only when the round lists the OSM ids
        out = os.path.join(folder, "street_carry.json")
        ids = [str(i) for i in (g.get("street_carry") or {}).get("osm_ids", [])]
        if not os.path.exists(out):
            sha_check(g, "street_carry.py")
            job(tid, "street_carry.py", [project(g["hwy"]), prev, r, out] + ids, g.get("env"))
        s = json.load(open(out))
        line = "; ".join(f"{k} {w.get('name') or ''}: not carried {w.get('not_carried_m', w.get('not_carried'))} m"
                         for k, w in (s.get("ways") or {}).items())
        return respond(req, output={"out": out, "line": line[:600] or "no ways"})
    if tid == "cl_gate":
        d = deps(req)
        ab, ls = json.load(open(d["cl_ab"]["out"])), json.load(open(d["cl_lost"]["out"]))
        rf = project(g["reasons"]) if g.get("reasons") else None
        reasons = json.load(open(rf)) if rf and os.path.exists(rf) else {}
        ok, s, rows = evaluate(ab, ls, reasons)
        rep = os.path.join(OUT, "combined_layer.json")
        json.dump({"round": r, "prev": prev, "hwy": g["hwy"], "tiles": g["tiles"], "reasons": g.get("reasons"),
                   "pass": ok, "summary": s, **rows}, open(rep, "w"), indent=0)
        det = (f"{r} vs {prev} on {g['hwy']}: new driver-height cells {s['drive_cells']} (unreasoned {s['unreasoned_drive_cells']}), "
               f"new aerial void cells {s['air_cells']} (unreasoned {s['unreasoned_air_cells']}), lost surface {s['lost_m2']} m2 "
               f"(unreasoned {s['unreasoned_lost_m2']} m2); all must be 0 unreasoned"
               + (f"; INCOMPLETE, can't pass: {'; '.join(s['incomplete'])}" if s["incomplete"] else "")
               + (f"; under deck (not gated, for the lead with frames): {s['under_deck_m2']} m2 in {s['under_deck_clusters']} clusters: "
                  + ", ".join(f"{c['tile']} {c['m2']} m2 deck {c['deck_over_b_min']} m over B" for c in rows["under_deck_clusters"][:8])
                  if s["under_deck_clusters"] else "; under deck 0 m2")
               + f"; rows and keys in {rep}" + (f"; reasons {g['reasons']}" if rf and os.path.exists(rf) else "; no reasons file yet"))
        items = ([f"drive {x['key']} ({'/'.join(x.get('classes', []))}, {x.get('rays', '?')} rays)" for x in rows["drive"] if not x["reason"]][:15]
                 + [f"air {x['key']} ({x.get('rays', '?')} rays)" for x in rows["air"] if not x["reason"]][:10]
                 + [f"lost {x['key']}: A {x.get('a')} -> B {x.get('b')}" for x in rows["lost"] if not x["reason"]][:15])
        return respond(req, output={"pass": str(ok).lower(), "detail": det, "items": json.dumps(items)})
    raise RuntimeError(f"combined: unknown step {tid}")


if __name__ == "__main__":
    run_main(main)
