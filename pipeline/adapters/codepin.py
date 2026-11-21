#!/usr/bin/env python3
"""Code freeze (the lead's standing rule, 21 Nov): the code a running round imports is frozen for that round.

code_pin (first step) records the sha256 of every script the round's kind may import, in <run>/code_pin.json.
code_verify (before the final accept) re-hashes them and FAILS the round if any changed, appeared or vanished.
Changes go to a new round. Hashes and names stay in the run folder (outside the repo).
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os

from common import load_request, project, respond, round_config, sha256, validate, OUT, run_main

EXT = (".py", ".mjs", ".js", ".sh", ".cjs")
DA = ["work/drive_audit"]   # the drive test's gate / scan scripts, used by terrain and page rounds
ROOTS = {
    "page_round": ["pe:phaseb", "pe:diag", "pe:reach"] + DA,
    "terrain_round_live": ["work/terrain_study/p2/tools", "work/terrain_study/tools"] + DA,
    "highway_round": ["highways/work/p2net"],
}


def files(cfg):
    roots = cfg.get("code_roots") or ROOTS.get(cfg.get("kind"), [])
    out = {}
    for r in roots:
        d = project(r)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):   # top level of each folder: the scripts themselves, not their data
            p = os.path.join(d, f)
            if f.endswith(EXT) and os.path.isfile(p):
                out[f"{r}/{f}"] = p
    for k in ("build_script",):          # a round's own build script
        if cfg.get(k):
            p = project(f"work/terrain_study/{cfg[k]}")
            if os.path.isfile(p):
                out[f"build/{cfg[k]}"] = p
    return out


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        return validate(req, c.get("pass", "true") == "true")
    cfg = round_config()
    pin = os.path.join(OUT, "code_pin.json")
    if tid == "code_pin":
        for rel, want in (cfg.get("require_sha") or {}).items():   # the round's rule must be the declared version
            got = sha256(project(rel))
            if got != want:
                return respond(req, output={"pass": "false", "detail": f"{os.path.basename(rel)} is {got[:12]}, the round "
                                            f"declares {want[:12]}: not the rule this round is for; nothing started"})
        if not os.path.exists(pin):   # pinned once per run; a retry never re-pins
            json.dump({k: sha256(p) for k, p in files(cfg).items()}, open(pin, "w"), indent=0)
        n = len(json.load(open(pin)))
        return respond(req, output={"pass": "true", "detail": f"{n} scripts pinned"})
    if tid == "code_verify":
        if not os.path.exists(pin):
            return respond(req, output={"pass": "false", "detail": "no code pin for this run"})
        old = json.load(open(pin))
        now = {k: sha256(p) for k, p in files(cfg).items()}
        changed = sorted(k for k in old if k in now and now[k] != old[k])
        gone = sorted(set(old) - set(now))
        new = sorted(set(now) - set(old))
        ok = not changed and not gone   # a new file nobody imports is harmless; it's listed
        det = (f"{len(old)} pinned scripts: {len(changed)} changed, {len(gone)} removed, {len(new)} added during the round"
               + (f"; changed: {', '.join(os.path.basename(k) for k in changed[:12])}" if changed else "")
               + (f"; removed: {', '.join(os.path.basename(k) for k in gone[:12])}" if gone else "")
               + ("" if ok else " -> code changed under the round: re-run on a new tag"))
        return respond(req, output={"pass": str(ok).lower(), "detail": det})
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    run_main(main)
