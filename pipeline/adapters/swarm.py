#!/usr/bin/env python3
"""Research swarm nodes (kind research_swarm): per item, helpers before / after a main research agent, a check gate,
and one report. Agents are sandboxed `claude -p` calls run as detached jobs (Irrigator ends a step at 120 s):
--restricted (no Bash, no settings files, file tools confined to the working folder plus read_dirs), dontAsk, and
Write/Edit allowed only on that item's own output files. Helpers get no write permission at all: their structured
answer is saved by this adapter. Each call has a USD cap and its real cost is reported to Irrigator.

Node ids: <item>__<helper>   a helper (when: before | after the main)
          <item>__main       the main agent: writes the item's files, returns the brief's structured summary
          <item>__check      the gate: the round's check command on the item's main output
          swarm_report       per-item minutes, cost, counts, open questions, helper findings
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import glob
import json
import os
import re
import shutil
import subprocess
import time

from common import OUT, Running, deps, load_request, project, respond, round_config, run_job, run_main, validate

CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
MAIN_SCHEMA = {"type": "object", "properties": {
    "card": {"type": "string"}, "summary": {"type": "string"},
    "conf_counts": {"type": "object", "properties": {k: {"type": "integer"} for k in "ABC"}, "required": list("ABC")},
    "open_questions": {"type": "array", "items": {"type": "string"}}, "minutes": {"type": "number"}},
    "required": ["card", "summary", "conf_counts", "open_questions"]}
HELPER_SCHEMA = {"type": "object", "properties": {
    "findings": {"type": "array", "items": {"type": "object", "properties": {
        "what": {"type": "string"}, "value": {"type": "string"}, "src": {"type": "string"},
        "conf": {"type": "string", "enum": ["A", "B", "C"]}}, "required": ["what", "value", "src"]}},
    "contradictions": {"type": "array", "items": {"type": "string"}},
    "notes": {"type": "string"}}, "required": ["findings"]}


def root(cfg):
    return project(cfg["root"])


def brief(cfg, ref):
    """ref = 'file.md' or 'file.md#Heading': the whole file or one '#'-heading section of it."""
    f, _, sec = ref.partition("#")
    t = open(os.path.join(root(cfg), f)).read()
    if not sec:
        return t
    parts = re.split(r"(?m)^(#+ .*)$", t)
    for i in range(1, len(parts), 2):
        if sec.lower() in parts[i].lower():
            return parts[i] + parts[i + 1]
    raise RuntimeError(f"no section '{sec}' in {f}")


def lanes(cfg):
    return [[str(x) for x in lane] for lane in cfg["lanes"]]


def prev_item(cfg, item):
    for lane in lanes(cfg):
        if item in lane and lane.index(item) > 0:
            return lane[lane.index(item) - 1]
    return None


def agent(name, prompt, cfg, model, budget, schema, writes=()):
    """Start or collect one sandboxed claude -p job; returns (structured output, cost_usd, minutes)."""
    r = root(cfg)
    allow = ["Read", "Glob", "Grep"] + (["WebFetch", "WebSearch"] if cfg.get("web", True) else [])
    allow += [f"{t}(./{w})" for w in writes for t in ("Write", "Edit")]
    tools = ",".join(["Read", "Glob", "Grep"] + (["WebFetch", "WebSearch"] if cfg.get("web", True) else [])
                     + (["Write", "Edit"] if writes else []))
    cmd = [CLAUDE, "-p", "--restricted", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
           "--model", model, "--tools", tools, "--permission-mode", "dontAsk", "--allowedTools", *allow,
           *sum((["--add-dir", project(d)] for d in cfg.get("read_dirs", [])), []),
           "--max-budget-usd", str(budget), "--output-format", "json", "--json-schema", json.dumps(schema), prompt]
    log = f"agents/{name}.json"
    os.makedirs(os.path.join(OUT, "agents"), exist_ok=True)
    rc = run_job(name, cmd, r, log)
    rec = json.load(open(os.path.join(OUT, "jobs", name, "job.json")))
    mins = round((os.path.getmtime(os.path.join(OUT, "jobs", name, "rc")) - rec["started"]) / 60, 1)
    try:
        d = json.load(open(os.path.join(OUT, log)))
    except ValueError:
        raise RuntimeError(f"{name}: claude rc={rc}, unparseable output (see {log})")
    cost = float(d.get("total_cost_usd") or 0)
    if d.get("is_error") or not d.get("structured_output"):
        raise RuntimeError(f"{name}: {d.get('subtype', 'error')}: {str(d.get('result'))[:200]} (cost {cost:.2f} USD)")
    return d["structured_output"], cost, mins, len(d.get("permission_denials") or [])


def check_state(item):
    """A check's verdict from the run state (the report doesn't depend on checks, so a failed one hides nothing)."""
    try:
        t = json.load(open(os.path.join(OUT, "run", "state.json")))["tasks"].get(f"{item}__check") or {}
    except (OSError, ValueError):
        return "unknown"
    res = t.get("result") or {}
    return res.get("detail") or f"{t.get('status', 'pending')}: {(t.get('error') or '')[:200]}"


def item_files(cfg, item):
    return [p.format(id=item) for p in cfg["main"]["writes"]]


def main():
    req = load_request()
    tid = req["task"]["id"]
    cfg = round_config()
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or req.get("output") or {}
        if tid.endswith("__check"):
            return validate(req, c.get("pass") == "true")
        return validate(req, True)
    d = deps(req)
    item, _, part = tid.partition("__")

    if tid == "swarm_report":
        rows = []
        for lane_i, lane in enumerate(lanes(cfg)):
            for it in lane:
                m = d.get(f"{it}__main", {})
                row = {"item": it, "lane": lane_i + 1, "check": check_state(it),
                       "main_minutes": float(m.get("minutes_wall", 0)), "main_cost_usd": float(m.get("cost_usd", 0)),
                       "conf_counts": json.loads(m.get("conf_counts") or "{}"),
                       "open_questions": json.loads(m.get("open_questions") or "[]"),
                       "summary": m.get("summary", "")}
                for h in cfg.get("helpers", []):
                    x = d.get(f"{it}__{h['id']}", {})
                    row[h["id"]] = {"minutes": float(x.get("minutes_wall", 0)), "cost_usd": float(x.get("cost_usd", 0)),
                                    "findings": int(x.get("findings", 0)), "contradictions": int(x.get("contradictions", 0))}
                row["total_cost_usd"] = round(row["main_cost_usd"] + sum(row[h["id"]]["cost_usd"] for h in cfg.get("helpers", [])), 2)
                rows.append(row)
        tot = round(sum(r["total_cost_usd"] for r in rows), 2)
        mins = sorted(r["main_minutes"] for r in rows)
        rep = {"round": cfg["round"], "items": len(rows), "total_cost_usd": tot,
               "cost_per_item_usd": round(tot / len(rows), 2) if rows else 0,
               "main_minutes_median": mins[len(mins) // 2] if mins else 0, "main_minutes_max": mins[-1] if mins else 0,
               "checks_passed": sum(1 for r in rows if r["check"].startswith("PASS")), "rows": rows}
        for p in (os.path.join(OUT, "swarm_report.json"), os.path.join(root(cfg), "calibration_report.json")):
            json.dump(rep, open(p, "w"), indent=1)
        return respond(req, output={"items": len(rows), "total_cost_usd": tot, "cost_per_item_usd": rep["cost_per_item_usd"],
                                    "main_minutes_median": rep["main_minutes_median"],
                                    "checks_passed": rep["checks_passed"], "report": os.path.join(OUT, "swarm_report.json")})

    if part == "check":
        files = [os.path.join(root(cfg), f) for f in item_files(cfg, item)]
        cmd = [project(x) if x.startswith(("pe:", "runs:")) else x for x in cfg["check"]]
        cmd = [c.format(id=item, card=files[0]) for c in cmd]
        p = subprocess.run(cmd, cwd=root(cfg), capture_output=True, text=True, timeout=100)
        why = (p.stdout.strip() or p.stderr.strip())[-600:]
        return respond(req, output={"pass": str(p.returncode == 0).lower(),
                                    "detail": ("PASS " if p.returncode == 0 else "FAIL ") + why})

    if part == "main":
        prev = prev_item(cfg, item)
        ctx = [f"You are researching item {item}. Write only these files (relative to your working folder): "
               + ", ".join(item_files(cfg, item)) + "."]
        if cfg.get("conventions"):
            ctx.append(f"Read {cfg['conventions']} first: it is the shared rulebook, read-only.")
        for h in cfg.get("helpers", []):
            if h.get("when", "before") == "before":
                ctx.append(f"A helper's findings for this item ({h['id']}) are in helpers/{item}__{h['id']}.json: "
                           "use them as leads, each needs its own source.")
        if prev:
            pm = d.get(f"{prev}__main", {})
            ctx.append(f"The previous item in your lane was {prev} (its card: {pm.get('card', '')}); its summary: "
                       f"{pm.get('summary', '')[:600]}. Reuse what you learned about sources and conventions.")
        prompt = brief(cfg, cfg["main"]["brief"]).replace("{id}", item) + "\n\n" + "\n".join(ctx)
        out, cost, mins, denied = agent(f"{item}__main", prompt, cfg, cfg["main"].get("model", "claude-opus-5-5"),
                                        cfg["main"].get("budget_usd", 5), MAIN_SCHEMA, writes=item_files(cfg, item))
        resp = {"card": out["card"], "summary": out["summary"], "conf_counts": json.dumps(out["conf_counts"]),
                "open_questions": json.dumps(out.get("open_questions", [])), "minutes_wall": mins,
                "minutes_reported": out.get("minutes", ""), "cost_usd": round(cost, 3), "denied_tool_calls": denied}
        r = {"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled",
             "output": {k: str(v) for k, v in resp.items()}, "cost_usd": cost}
        return json.dump(r, _sys.stdout)

    h = next(x for x in cfg.get("helpers", []) if x["id"] == part)
    ctx = [f"Item: {item}. You only read and report; you write nothing."]
    if cfg.get("conventions"):
        ctx.append(f"Shared rules: {cfg['conventions']} (read-only).")
    if h.get("when") == "after":
        ctx.append(f"Check the researched files for this item: {', '.join(item_files(cfg, item))}.")
    prompt = brief(cfg, h["brief"]).replace("{id}", item) + "\n\n" + "\n".join(ctx)
    out, cost, mins, denied = agent(f"{item}__{part}", prompt, cfg, h.get("model", "claude-sonnet-5-5"),
                                    h.get("budget_usd", 2), HELPER_SCHEMA)
    os.makedirs(os.path.join(root(cfg), "helpers"), exist_ok=True)
    json.dump(out, open(os.path.join(root(cfg), "helpers", f"{item}__{part}.json"), "w"), indent=1)
    resp = {"findings": len(out.get("findings", [])), "contradictions": len(out.get("contradictions", [])),
            "minutes_wall": mins, "cost_usd": round(cost, 3), "file": f"helpers/{item}__{part}.json"}
    r = {"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled",
         "output": {k: str(v) for k, v in resp.items()}, "cost_usd": cost}
    return json.dump(r, _sys.stdout)


if __name__ == "__main__":
    run_main(main)
