#!/usr/bin/env python3
"""Research swarm on Claude with a per-step working folder (a research_swarm round with "adapter": "swarm_claude" and
"engine": "claude_ws"). Owner, 2026-11-28, during the 151 h Gemini cap: "Sonnet for all steps", and "Yes, Street View
too" (Claude may read the Street View captures). A new file beside swarm.py / swarm_ext.py, which stay unchanged.

Each step runs exactly as an agy step of swarm_ext.py does (its own scratch folder with copies of the item's files,
copied back after; the round's pre-step gives it sv/, ./capture.sh and ./view.sh; heavy-job slots; deleted afterwards),
but the agent is a `claude -p` call:
  --restricted (file tools confined to the step folder plus read_dirs), --setting-sources "", --strict-mcp-config
  (no MCP, no browser), --no-session-persistence (no transcript, so no image copies outlive the folder), dontAsk;
  Read, Glob, Grep, WebSearch, WebFetch; Write/Edit only on the item's own files and on plan*.json (Street View plans);
  Bash only for ./capture.sh and ./view.sh; the round's deny_read paths denied to Read; a USD cap per call.
Round options: "claude_cores" (heavy-job cores per call, default 2); main/helper "budget_usd" (defaults 5 / 2).
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os
import shutil

import prestep
import swarm as S
from common import OUT, load_request, project, round_config, run_main

WS = os.path.join(os.path.realpath("/tmp"), "five-boroughs-cl")   # the real temp path, outside the home folder
ENGINE = "claude_ws"


def budget(cfg, name):
    part = name.partition("__")[2]
    if part in ("main", "fix"):
        return cfg["main"].get("budget_usd", 5)
    h = next((x for x in cfg.get("helpers", []) if x["id"] == part), {})
    return h.get("budget_usd", 2)


def command(cfg, name, prompt, model, schema, files):
    writes = [os.path.basename(f) for f in files] + ["plan*.json"]
    tools = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Write", "Edit", "Bash"]
    allow = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"] + [f"{t}(./{w})" for w in writes for t in ("Write", "Edit")]
    allow += ["Bash(./capture.sh:*)", "Bash(./view.sh:*)"]
    deny = []
    for d in cfg.get("deny_read", []):
        p = project(d).rstrip("/")
        deny += [f"Read(/{p}/**)", f"Read(/{p})"]   # '//' + absolute path in Claude Code permission rules
    return ([S.CLAUDE, "-p", "--restricted", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
             "--model", model, "--tools", ",".join(tools), "--permission-mode", "dontAsk", "--allowedTools", *allow]
            + (["--disallowedTools", *deny] if deny else [])
            + sum((["--add-dir", project(d)] for d in cfg.get("read_dirs", [])), [])
            + ["--max-budget-usd", str(budget(cfg, name)), "--output-format", "json", "--json-schema", json.dumps(schema),
               prompt])


def agent_claude_ws(name, prompt, cfg, model, schema, files=()):
    """swarm.agent_agy's contract (files copied in and back, the folder deleted) with a claude -p job."""
    ws = os.path.join(WS, os.path.basename(OUT), name)
    log = f"agents/{name}.json"
    os.makedirs(os.path.join(OUT, "agents"), exist_ok=True)
    if not os.path.exists(os.path.join(OUT, "jobs", name, "job.json")):
        shutil.rmtree(ws, ignore_errors=True)
        os.makedirs(ws)
        for f in files:
            if os.path.exists(os.path.join(S.root(cfg), f)):
                shutil.copy2(os.path.join(S.root(cfg), f), os.path.join(ws, os.path.basename(f)))
    rc = S.run_job(name, command(cfg, name, prompt, model, schema, files), ws, log,
                   cores=int(cfg.get("claude_cores", 2)), check_load=False)
    rec = json.load(open(os.path.join(OUT, "jobs", name, "job.json")))
    mins = round((os.path.getmtime(os.path.join(OUT, "jobs", name, "rc")) - rec["started"]) / 60, 1)
    try:
        d = json.load(open(os.path.join(OUT, log)))
    except ValueError:
        raise RuntimeError(f"{name}: claude rc={rc}, unparseable output (see {log})")
    cost = float(d.get("total_cost_usd") or 0)
    u = d.get("usage") or {}
    S.TOKENS[name] = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens"))
    if d.get("is_error") or not isinstance(d.get("structured_output"), dict):
        raise RuntimeError(f"{name}: {d.get('subtype', 'error')}: {str(d.get('result'))[:200]} (cost {cost:.2f} USD)")
    for f in files:
        src = os.path.join(ws, os.path.basename(f))
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(S.root(cfg), f))
    shutil.rmtree(ws, ignore_errors=True)
    return d["structured_output"], cost, mins, len(d.get("permission_denials") or [])


def main():
    req = load_request()
    cfg = round_config()
    if cfg.get("engine") != ENGINE:
        raise RuntimeError(f"swarm_claude needs \"engine\": \"{ENGINE}\" (got {cfg.get('engine')!r})")
    cfg = json.loads(json.dumps(cfg))
    cfg["engine"] = "agy"   # swarm.py's working-folder behaviour (file names, copies); the call itself is claude
    part = req["task"]["id"].partition("__")[2]
    if part == "fix" and cfg.get("fix_brief"):
        cfg["main"]["brief"] = cfg["fix_brief"]
        for v in (cfg.get("vars") or {}).values():
            if isinstance(v, dict):
                v.pop("_brief_main", None)
    S.agent_agy = agent_claude_ws
    S.load_request = lambda: req
    S.round_config = lambda: cfg
    item = req["task"]["id"].partition("__")[0]
    if "valid" not in (req.get("kind") or "") and req["task"]["id"] != "swarm_report":
        prestep.wrap(S, cfg, item, part)
    out = S.main()
    if part == "check":
        prestep.done(cfg, item)
    return out


if __name__ == "__main__":
    run_main(main)
