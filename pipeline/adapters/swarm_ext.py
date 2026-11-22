#!/usr/bin/env python3
"""Research swarm with round-level options (a research_swarm round with "adapter": "swarm_ext"). A new file beside
swarm.py, which it runs unchanged (swarm.py is pinned by running rounds). Options:

  "allow_google_imagery": true   agy may read Google imagery hosts (owner, 2026-11-22: "you can now use street view to
                                 gather photos"; owner-approved Google allowlist). The other denies stay.
  "fix_brief": "file.md#Sections"  the fix pass gets this brief instead of the main brief.
  "prestep": {"module": ...}     a module outside the repo prepares each step's working folder: see prestep.py.

Everything else is swarm.py's behaviour: same node ids, outputs and checks.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json

import agy_cleanup
import prestep
import swarm as S
from common import load_request, round_config, run_main


def main():
    req = load_request()
    cfg = round_config()
    agy_cleanup.wrap(S)   # each agy call's own ~/.gemini brain folder goes after its step (owner's OK, 2026-11-22)
    if cfg.get("allow_google_imagery"):
        S.DENY_HOSTS[:] = []   # agy_settings reads it per call
    part = req["task"]["id"].partition("__")[2]
    if part == "fix" and cfg.get("fix_brief"):
        cfg = json.loads(json.dumps(cfg))
        cfg["main"]["brief"] = cfg["fix_brief"]
        for v in (cfg.get("vars") or {}).values():   # a per-item main brief would win in item_ref; the fix brief wins here
            if isinstance(v, dict):
                v.pop("_brief_main", None)
    S.load_request = lambda: req   # swarm.main reads the request and the round through its own names
    S.round_config = lambda: cfg
    item = req["task"]["id"].partition("__")[0]
    if "valid" not in (req.get("kind") or "") and req["task"]["id"] != "swarm_report":
        prestep.wrap(S, cfg, item, part)   # the round's pre-step, if any, prepares this step's working folder
    out = S.main()
    if part == "check":
        prestep.done(cfg, item)
    return out


if __name__ == "__main__":
    run_main(main)
