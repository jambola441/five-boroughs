#!/usr/bin/env python3
"""Lane guard for terrain rounds (register row 27, "Terrain surface above a highway lane"; the lead, 2026-11-21).
A round opts in with a "lane_guard" block:
  {"lanes": "highways/work/p2net/final_fdr/<lane set>",   the pinned lane folder (lanes_*.json)
   "lanes_sha": "<sha256>",                                its pinned manifest hash (see manifest_sha below)
   "prev": "<previous frozen terrain round>"}              default: the round's prev
Steps (after t_freeze):
  lg_manifest      stop: the lane folder's manifest hash must equal lanes_sha
  lg_over_this     job: the terrain study's v1c_terrain_over_lanes.py on this round   (about 3 min)
  lg_over_prev     job: the same on the previous round, same lanes                     (about 3 min)
  lg_edges         job: v1c_lane_edge_drops.py, this round vs the previous one         (about 3 min)
  lg_gate_over     stop: any NEW triangle > 0.5 m over a lane (matched by centroid), or any depth band over 0.5 m
                   (0.5-2 / 2-5 / >5 m) that rises vs the previous round; the 0.1-0.5 m band is reported only
  lg_gate_edges    stop: any new lane-edge gap > 0.5 m
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import glob
import hashlib
import json
import os

from common import OUT, deps, load_request, project, respond, round_config, run_job, run_main, validate

TOOLS = "work/terrain_study/p2/tools"
CHECKS = "work/terrain_study/p2/checks"
BANDS = ("0.5-2", "2-5", ">5")


def manifest_sha(lanes_dir):
    """sha256 over the lane folder's lanes_*.json files: name and content, in name order."""
    h = hashlib.sha256()
    for f in sorted(glob.glob(os.path.join(lanes_dir, "lanes_*.json"))):
        h.update(os.path.basename(f).encode() + b"\0" + open(f, "rb").read() + b"\0")
    return h.hexdigest()


def lg(cfg):
    g = dict(cfg["lane_guard"])
    g.setdefault("prev", cfg["prev"])
    return g


def py():
    return project(".venv/bin/python")


def job(name, script, env):
    rc = run_job(name, [py(), "-I", project(f"{TOOLS}/{script}")], project(TOOLS), f"{name}.log",
                 env={**env, "LANES": project(env["LANES"])})
    if rc != 0:
        raise RuntimeError(f"{name}: {script} exited {rc} (see {name}.log)")


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        return validate(req, c.get("pass", "true") == "true")
    cfg = round_config()
    g = lg(cfg)
    r, prev = cfg["round"], g["prev"]

    if tid == "lg_manifest":
        sha = manifest_sha(project(g["lanes"]))
        n = len(glob.glob(os.path.join(project(g["lanes"]), "lanes_*.json")))
        ok = sha == g.get("lanes_sha")
        return respond(req, output={"pass": str(ok).lower(), "detail": f"lane manifest {g['lanes']} ({n} lane files): sha {sha[:16]} "
                                    + ("matches the pinned one" if ok else f"!= pinned {str(g.get('lanes_sha'))[:16]} (stop)")})
    if tid in ("lg_over_this", "lg_over_prev"):
        rev = r if tid == "lg_over_this" else prev
        out = os.path.join(OUT, "laneguard", f"{tid}.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if not os.path.exists(out):
            job(tid, "v1c_terrain_over_lanes.py", {"R6N": rev, "LANES": g["lanes"], "OUT": out})
        s = json.load(open(out))["summary"]
        return respond(req, output={"out": out, "triangles": s["triangles"], "by_depth": json.dumps(s["by_depth"])})
    if tid == "lg_edges":
        out = project(f"{CHECKS}/{r}_lane_edge_drops.json")
        mark = os.path.join(OUT, "lg_edges.done")
        if not os.path.exists(mark):   # the script writes into the terrain study's checks folder; run it for this round
            job(tid, "v1c_lane_edge_drops.py", {"R6N": r, "BASE": prev, "LANES": g["lanes"]})
            open(mark, "w").write(out)
        s = json.load(open(out))
        return respond(req, output={"out": out, "new_edge_gaps": s["new_edge_gaps"]})

    d = deps(req)
    if tid == "lg_gate_over":
        new, old = (json.load(open(d[k]["out"])) for k in ("lg_over_this", "lg_over_prev"))
        key = lambda t: (round(t["x"], 1), round(t["z"], 1))
        old_deep = {key(t) for t in old["worst"] if t["poke"] > 0.5}
        fresh = [t for t in new["worst"] if t["poke"] > 0.5 and key(t) not in old_deep]
        bn, bo = new["summary"]["by_depth"], old["summary"]["by_depth"]
        rose = [f"{b} m: {bo.get(b, 0)} -> {bn.get(b, 0)}" for b in BANDS if bn.get(b, 0) > bo.get(b, 0)]
        ok = not fresh and not rose
        det = (f"new triangles > 0.5 m over a lane: {len(fresh)} (must be 0); depth bands vs {prev}: "
               + ", ".join(f"{b} m {bo.get(b, 0)} -> {bn.get(b, 0)}" for b in BANDS)
               + (f" (rose: {'; '.join(rose)})" if rose else " (none rose)")
               + f"; 0.1-0.5 m band (reported): {bo.get('0.1-0.5', 0)} -> {bn.get('0.1-0.5', 0)}; lanes {lg(cfg)['lanes']}")
        items = [f"{t['tile']} {t['road'] or t['way']} ({t['kind']}) at {t['x']},{t['z']}: {t['poke']} m over the lane" for t in fresh[:40]]
        return respond(req, output={"pass": str(ok).lower(), "detail": det, "items": json.dumps(items)})
    if tid == "lg_gate_edges":
        s = json.load(open(d["lg_edges"]["out"]))
        n = int(s["new_edge_gaps"])
        items = [f"{w[3] or w[4]} ({w[2]}) at {w[5]},{w[6]}: gap {w[0]} m (was {w[1]} m)" for w in s.get("worst", [])[:20]]
        return respond(req, output={"pass": str(n == 0).lower(),
                                    "detail": f"new lane-edge gaps > 0.5 m vs {prev}: {n} (must be 0); by road {json.dumps(s.get('by_road', {}))[:300]}",
                                    "items": json.dumps(items)})
    raise RuntimeError(f"laneguard: unknown step {tid}")


if __name__ == "__main__":
    run_main(main)
