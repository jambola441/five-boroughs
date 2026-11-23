#!/usr/bin/env python3
"""Code freeze (the lead's standing rule, 21 Nov): the code a running round imports is frozen for that round.

code_pin (first step) records the sha256 of every script the round's kind may import, in <run>/code_pin.json.
code_verify (before the final accept) re-hashes them and FAILS the round if any changed, appeared or vanished.
Changes go to a new round, except a lead-approved mid-round patch (the lead, 2026-11-23, PROPOSAL_round_flexibility.md
option 1): a record <run>/PATCH_<n>.json that code_verify accepts only when it matches exactly:
  {"by": "lead", "record": "<the lead's .md in the run folder>", "reason": "...", "test_copy": "<where it was proven>",
   "loosens_gate": false | true, "gate": "<which gate and how, when true>",
   "changes": [{"path": "<pinned key>", "old_sha256": "<the pin, or the previous patch's new>", "new_sha256": "..."}]}
Patches apply in PATCH_<n> order; a file's current sha must equal its last patch's new_sha256. Anything else that
changed still fails the round. Accepted patches are listed in the detail (and so at the lead gate), with any loosened gate.
Hashes and names stay in the run folder (outside the repo).
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os

from common import load_request, project, respond, round_config, sha256, validate, OUT, run_main

EXT = (".py", ".mjs", ".js", ".sh", ".cjs")
DA = ["work/drive_audit"]   # the drive test's gate / scan scripts, used by terrain and page rounds
ROOTS = {
    "page_round": ["pe:phaseb", "pe:diag", "pe:reach", "pe:guards"] + DA,
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


def patched(pin):
    """The shas code_verify expects after the lead's PATCH_<n>.json records; returns (expect, notes, refused)."""
    import glob, re
    expect, notes, bad = dict(pin), [], []
    recs = sorted(glob.glob(os.path.join(OUT, "PATCH_*.json")),
                  key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    for p in recs:
        name = os.path.basename(p)
        try:
            r = json.load(open(p))
        except (OSError, ValueError) as e:
            bad.append(f"{name}: unreadable ({e})"[:160])
            continue
        why = []
        if r.get("by") != "lead":
            why.append("by is not 'lead'")
        rec = str(r.get("record") or "")
        if not rec or not os.path.isfile(os.path.join(OUT, rec)):
            why.append("no lead record file in the run folder")
        if not r.get("reason"):
            why.append("no reason")
        if not r.get("test_copy"):
            why.append("not proven on a test copy (test_copy)")
        if not isinstance(r.get("loosens_gate"), bool):
            why.append("loosens_gate must be true or false")
        elif r["loosens_gate"] and not r.get("gate"):
            why.append("loosens_gate true needs 'gate'")
        ch = r.get("changes")
        if not isinstance(ch, list) or not ch:
            why.append("no changes listed")
            ch = []
        for c in ch:
            k = c.get("path") if isinstance(c, dict) else None
            if k is None or k not in expect and c.get("old_sha256") is not None:
                why.append(f"{k}: not a pinned script")
            elif expect.get(k) != c.get("old_sha256"):
                why.append(f"{os.path.basename(str(k))}: old_sha256 isn't the pinned (or previously patched) sha")
        if why:
            bad.append(f"{name}: " + ", ".join(why))
            continue
        for c in ch:
            expect[c["path"]] = c.get("new_sha256")
        notes.append(f"{name} ({', '.join(os.path.basename(c['path']) for c in ch)}; {r['reason'][:80]}"
                     + (f"; LOOSENS GATE: {r['gate'][:80]}" if r["loosens_gate"] else "") + ")")
    return expect, notes, bad


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
        expect, notes, bad = patched(old)
        changed = sorted(k for k in expect if k in now and now[k] != expect[k])
        gone = sorted(k for k in set(expect) - set(now) if expect[k] is not None)
        new = sorted(set(now) - set(old))
        ok = not changed and not gone and not bad   # a new file nobody imports is harmless; it's listed
        det = (f"{len(old)} pinned scripts: {len(changed)} changed, {len(gone)} removed, {len(new)} added during the round"
               + (f"; lead patches accepted: {'; '.join(notes)}" if notes else "")
               + (f"; patch records refused: {'; '.join(bad)}" if bad else "")
               + (f"; changed: {', '.join(os.path.basename(k) for k in changed[:12])}" if changed else "")
               + (f"; removed: {', '.join(os.path.basename(k) for k in gone[:12])}" if gone else "")
               + ("" if ok else " -> code changed under the round: re-run on a new tag"))
        return respond(req, output={"pass": str(ok).lower(), "detail": det})
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    run_main(main)
