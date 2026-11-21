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
    "hints_conf": {"type": "object", "properties": {k: {"type": "integer"} for k in "ABC"}},
    "open_questions": {"type": "array", "items": {"type": "string"}}, "minutes": {"type": "number"},
    "helpers": {"type": "string"}, "check": {"type": "string"}},
    "required": ["card", "summary", "conf_counts", "open_questions", "minutes", "check"]}
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
    f, _, secs = ref.partition("#")
    t = open(os.path.join(root(cfg), f)).read()
    if not secs:
        return t
    parts, out = re.split(r"(?m)^(#+ .*)$", t), []
    for sec in secs.split("+"):   # 'file.md#Rules+(a) Photo': several sections, in this order
        hit = [parts[i] + parts[i + 1] for i in range(1, len(parts), 2) if sec.lower() in parts[i].lower()]
        if not hit:
            raise RuntimeError(f"no section '{sec}' in {f}")
        out.append(hit[0])
    return "\n".join(out)


def item_ref(cfg, item, key, default):
    """A per-item override of a brief or conventions file (vars[item]['_<key>']), else the round's default."""
    return (cfg.get("vars", {}).get(item) or {}).get("_" + key) or default


def fill(cfg, item, text):
    """{id} and the item's own placeholders (cfg vars[item], e.g. {COMPLEX_ID} {NAME} {LANE} {TURN} {PATTERN})."""
    for k, v in {"id": item, "NYC_ROOT": project(".").rstrip("/.").rstrip("/"), **(cfg.get("vars", {}).get(item) or {})}.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def lanes(cfg):
    return [[str(x) for x in lane] for lane in cfg["lanes"]]


def prev_item(cfg, item):
    for lane in lanes(cfg):
        if item in lane and lane.index(item) > 0:
            return lane[lane.index(item) - 1]
    return None


TOKENS = {}   # agent name -> total tokens (agy reports tokens, not USD: its use comes out of the Gemini quota)
AGY_WS = "/tmp/five-boroughs-agy"   # outside the home folder: agy may write only here
DENY_HOSTS = ["google.com", "googleapis.com", "ggpht.com", "googleusercontent.com", "gstatic.com"]   # no Google imagery to agy


def agy_settings(cfg):
    """agy permissions (Deny > Ask > Allow): writes anywhere under the home folder denied (the agent's folder is outside
    it); secrets, credentials and page code unreadable; the round's read_dirs readable; web reading open except Google
    imagery hosts. Commands run in agy's terminal sandbox (fs: workspace, temp and read grants only)."""
    H, N = os.path.expanduser("~"), project(".").rstrip("/.").rstrip("/")
    deny = [f"write_file({H})"] + [f"read_file({H}/{d})" for d in (".config", ".ssh", ".aws", ".gnupg", ".claude", ".gemini",
                                                                  ".local", "Library", ".netrc", ".zsh_history")]
    deny += [f"read_file({project(d)})" for d in cfg.get("deny_read", ["pe:"])] + [f"read_url({h})" for h in DENY_HOSTS]
    allow = [f"read_file({project(d)})" for d in cfg.get("read_dirs", [])] + [f"read_file({root(cfg)})", "read_url(*)"]
    return json.dumps({"permissions": {"allow": allow, "deny": deny}})


def agent_agy(name, prompt, cfg, model, schema, files=()):
    """One agy -p job in its own folder under AGY_WS. files: the item's files, copied in from the root first and copied
    back after; everything else the agent fetched there (pages, photos) is deleted with the folder."""
    ws = os.path.join(AGY_WS, os.path.basename(OUT), name)
    log = f"agents/{name}.json"
    os.makedirs(os.path.join(OUT, "agents"), exist_ok=True)
    if not os.path.exists(os.path.join(OUT, "jobs", name, "job.json")):
        shutil.rmtree(ws, ignore_errors=True)
        os.makedirs(ws)
        for f in files:
            if os.path.exists(os.path.join(root(cfg), f)):
                shutil.copy2(os.path.join(root(cfg), f), os.path.join(ws, os.path.basename(f)))
    cmd = ["agy", "-p", prompt, "--model", model, "--sandbox", "--settings", agy_settings(cfg), "--output-format", "json",
           "--json-schema", json.dumps(schema), "--print-timeout", str(cfg.get("agy_timeout_s", 5400)) + "s"]
    rc = run_job(name, cmd, ws, log)
    rec = json.load(open(os.path.join(OUT, "jobs", name, "job.json")))
    mins = round((os.path.getmtime(os.path.join(OUT, "jobs", name, "rc")) - rec["started"]) / 60, 1)
    try:
        d = json.load(open(os.path.join(OUT, log)))
    except ValueError:
        raise RuntimeError(f"{name}: agy rc={rc}, unparseable output (see {log})")
    TOKENS[name] = (d.get("usage") or {}).get("total_tokens", 0)
    resp = d.get("response") or ""
    try:
        out = json.loads(resp)
    except ValueError:
        m = re.findall(r"```json\s*(\{.*?\})\s*```", resp, re.S)
        out = json.loads(m[-1]) if m else None
    if d.get("status") != "SUCCESS" or not isinstance(out, dict):
        raise RuntimeError(f"{name}: agy status {d.get('status')}, no structured output: {resp[:200]}")
    for f in files:   # the item's files back into the root; the folder (and anything fetched into it) goes
        src = os.path.join(ws, os.path.basename(f))
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(root(cfg), f))
    shutil.rmtree(ws, ignore_errors=True)
    return out, 0.0, mins, 0


def agent(name, prompt, cfg, model, budget, schema, writes=(), bash=(), subagents=False):
    """Start or collect one sandboxed claude -p job; returns (structured output, cost_usd, minutes, denials).
    bash: allowed command prefixes only (e.g. 'date'); subagents: the Agent tool (in-turn helpers; they can't write).
    A round with engine 'agy' runs agent_agy instead (writes = the files copied in and out)."""
    if cfg.get("engine") == "agy":
        return agent_agy(name, prompt, cfg, model, schema, files=writes)
    r = root(cfg)
    base = ["Read", "Glob", "Grep"] + (["WebFetch", "WebSearch"] if cfg.get("web", True) else []) + (["Agent"] if subagents else [])
    allow = base + [f"{t}(./{w})" for w in writes for t in ("Write", "Edit")] + [f"Bash({b}:*)" for b in bash]
    tools = ",".join(base + (["Write", "Edit"] if writes else []) + (["Bash"] if bash else []))
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


def run_check(cfg, item):
    files = [os.path.join(root(cfg), f) for f in item_files(cfg, item)]
    cmd = [project(x) if x.startswith(("pe:", "runs:")) else x for x in cfg["check"]]
    cmd = [fill(cfg, item, c).replace("{card}", files[0]) for c in cmd]
    p = subprocess.run(cmd, cwd=root(cfg), capture_output=True, text=True, timeout=100)
    return p.returncode == 0, (p.stdout.strip() or p.stderr.strip())


def nonempty(d):
    return {k: (str(v) if str(v).strip() else "none") for k, v in d.items()}


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
                ok, why = run_check(cfg, it)   # live: the report may run while the last checks are still going
                row = {"item": it, "lane": lane_i + 1, "check": ("PASS " if ok else "FAIL ") + why[-300:],
                       "check_step": check_state(it)[:200],
                       "main_minutes": float(m.get("minutes_wall", 0)), "main_cost_usd": float(m.get("cost_usd", 0)),
                       "minutes_reported": m.get("minutes_reported", ""), "hints_conf": json.loads(m.get("hints_conf") or "{}"),
                       "helpers": m.get("helpers", ""), "denied_tool_calls": m.get("denied_tool_calls", ""),
                       "main_tokens": int(m.get("tokens", 0) or 0),
                       "fix": {k: d.get(f"{it}__fix", {}).get(k) for k in ("ran", "gate_before", "contradictions_in",
                                                                          "minutes_wall", "tokens")} if cfg.get("fix") else None,
                       "conf_counts": json.loads(m.get("conf_counts") or "{}"),
                       "open_questions": json.loads(m.get("open_questions") or "[]"),
                       "summary": m.get("summary", "")}
                for h in cfg.get("helpers", []):
                    x = d.get(f"{it}__{h['id']}", {})
                    row[h["id"]] = {"minutes": float(x.get("minutes_wall", 0)), "cost_usd": float(x.get("cost_usd", 0)),
                                    "tokens": int(x.get("tokens", 0) or 0),
                                    "findings": int(x.get("findings", 0)), "contradictions": int(x.get("contradictions", 0))}
                row["total_cost_usd"] = round(row["main_cost_usd"] + sum(row[h["id"]]["cost_usd"] for h in cfg.get("helpers", [])), 2)
                rows.append(row)
        tot = round(sum(r["total_cost_usd"] for r in rows), 2)
        mins = sorted(r["main_minutes"] for r in rows)
        rep = {"round": cfg["round"], "items": len(rows), "total_cost_usd": tot,
               "cost_per_item_usd": round(tot / len(rows), 2) if rows else 0,
               "main_minutes_median": mins[len(mins) // 2] if mins else 0, "main_minutes_max": mins[-1] if mins else 0,
               "checks_passed": sum(1 for r in rows if r["check"].startswith("PASS")),
               "tokens_total": sum(r.get("main_tokens", 0) + sum((r.get(h["id"]) or {}).get("tokens", 0) for h in cfg.get("helpers", []))
                                   + int(((r.get("fix") or {}).get("tokens") or 0)) for r in rows), "rows": rows}
        for p in (os.path.join(OUT, "swarm_report.json"), os.path.join(root(cfg), "calibration_report.json")):
            json.dump(rep, open(p, "w"), indent=1)
        return respond(req, output={"items": len(rows), "total_cost_usd": tot, "cost_per_item_usd": rep["cost_per_item_usd"],
                                    "main_minutes_median": rep["main_minutes_median"],
                                    "checks_passed": rep["checks_passed"], "report": os.path.join(OUT, "swarm_report.json")})

    if part == "check":
        ok, why = run_check(cfg, item)
        return respond(req, output={"pass": str(ok).lower(), "detail": ("PASS " if ok else "FAIL ") + why[-600:]})

    if part == "main":
        prev = prev_item(cfg, item)
        agy = cfg.get("engine") == "agy"
        ctx = [f"You are researching item {item}. Write only these files (relative to your working folder): "
               + ", ".join(os.path.basename(f) if agy else f for f in item_files(cfg, item))
               + (" (copies of the cards in " + root(cfg) + "/cards, already in your working folder: edit them in place; "
                  "the graph copies them back)" if agy else "") + "."]
        if cfg.get("conventions"):
            conv = item_ref(cfg, item, "conventions", cfg["conventions"])
            ctx.append(f"Read {os.path.join(root(cfg), conv)} first: it is the shared rulebook, read-only.")
        for h in cfg.get("helpers", []):
            if h.get("when", "before") == "before":
                ctx.append(f"A helper's findings for this item ({h['id']}) are in {root(cfg)}/helpers/{item}__{h['id']}.json: "
                           "use them as leads, each needs its own source.")
        if prev:
            pm = d.get(f"{prev}__main", {})
            ctx.append(f"The previous item in your lane was {prev} (its card: {root(cfg)}/{item_files(cfg, prev)[0]}); its summary: "
                       f"{pm.get('summary', '')[:600]}. Reuse what you learned about sources and conventions.")
        if cfg["main"].get("output_note"):
            ctx.append(fill(cfg, item, cfg["main"]["output_note"]))
        prompt = fill(cfg, item, brief(cfg, item_ref(cfg, item, "brief_main", cfg["main"]["brief"]))) + "\n\n" + "\n".join(ctx)
        out, cost, mins, denied = agent(f"{item}__main", prompt, cfg, cfg["main"].get("model", "claude-opus-5-5"),
                                        cfg["main"].get("budget_usd", 5), MAIN_SCHEMA, writes=item_files(cfg, item),
                                        bash=[fill(cfg, item, b) for b in cfg["main"].get("bash_allow", [])],
                                        subagents=cfg["main"].get("subagents", False))
        resp = {"card": out["card"], "summary": out["summary"], "conf_counts": json.dumps(out["conf_counts"]),
                "open_questions": json.dumps(out.get("open_questions", [])), "minutes_wall": mins,
                "minutes_reported": out.get("minutes", ""), "cost_usd": round(cost, 3), "denied_tool_calls": denied,
                "hints_conf": json.dumps(out.get("hints_conf", {})), "helpers": out.get("helpers", ""),
                "self_check": out.get("check", ""), "tokens": TOKENS.get(f"{item}__main", 0)}
        r = {"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled",
             "output": nonempty(resp), "cost_usd": cost}
        return json.dump(r, _sys.stdout)

    if part == "fix":   # the gate's result and the after-helpers' contradictions back to the main agent, once
        gate = run_check(cfg, item)
        contra = []
        for hh in cfg.get("helpers", []):
            f = os.path.join(root(cfg), "helpers", f"{item}__{hh['id']}.json")
            if hh.get("when") == "after" and os.path.exists(f):
                contra += json.load(open(f)).get("contradictions", [])
        base = {"gate_before": ("PASS " if gate[0] else "FAIL ") + gate[1][:300], "contradictions_in": len(contra)}
        if gate[0] and not contra:
            return respond(req, output={**base, "ran": "false", "minutes_wall": 0, "cost_usd": 0, "tokens": 0})
        prompt = (fill(cfg, item, brief(cfg, item_ref(cfg, item, "brief_main", cfg["main"]["brief"]))) + "\n\nFIX PASS for item " + item + ". "
                  + fill(cfg, item, cfg["main"].get("output_note") or "") + "\nThe gate (check_card.py) said:\n" + gate[1][:2000]
                  + "\nThe fact checker returned these contradictions (resolve each: correct it, or record the "
                  "conflict and add an open question):\n" + "\n".join("- " + c for c in contra)[:6000]
                  + "\nEdit the card in your working folder until the gate would pass, then return the turn output.")
        out, cost, mins, _ = agent(f"{item}__fix", prompt, cfg, cfg["main"].get("model", "claude-opus-5-5"),
                                   cfg["main"].get("budget_usd", 5), MAIN_SCHEMA, writes=item_files(cfg, item))
        r = {"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled", "cost_usd": cost,
             "output": nonempty({**base, "ran": "true", "minutes_wall": mins, "cost_usd": round(cost, 3),
                                 "tokens": TOKENS.get(f"{item}__fix", 0)})}
        return json.dump(r, _sys.stdout)

    h = next(x for x in cfg.get("helpers", []) if x["id"] == part)
    ctx = [f"Item: {item}. You only read and report; you write nothing."]
    if cfg.get("conventions"):
        ctx.append(f"Shared rules: {os.path.join(root(cfg), item_ref(cfg, item, 'conventions', cfg['conventions']))} (read-only).")
    if h.get("output_note"):
        ctx.append(fill(cfg, item, h["output_note"]))
    if h.get("when") == "after":
        ctx.append(f"Check the researched files for this item: {', '.join(os.path.join(root(cfg), f) for f in item_files(cfg, item))}.")
    hb = h["brief"]
    if item_ref(cfg, item, "brief_helpers", None):   # same sections, the item's own helper file
        hb = item_ref(cfg, item, "brief_helpers", None) + "#" + hb.partition("#")[2]
    prompt = fill(cfg, item, brief(cfg, hb)) + "\n\n" + "\n".join(ctx)
    out, cost, mins, denied = agent(f"{item}__{part}", prompt, cfg, h.get("model", "claude-sonnet-5-5"),
                                    h.get("budget_usd", 2), HELPER_SCHEMA)
    os.makedirs(os.path.join(root(cfg), "helpers"), exist_ok=True)
    json.dump(out, open(os.path.join(root(cfg), "helpers", f"{item}__{part}.json"), "w"), indent=1)
    resp = {"findings": len(out.get("findings", [])), "contradictions": len(out.get("contradictions", [])),
            "minutes_wall": mins, "cost_usd": round(cost, 3), "file": f"helpers/{item}__{part}.json",
            "tokens": TOKENS.get(f"{item}__{part}", 0)}
    r = {"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled",
         "output": nonempty(resp), "cost_usd": cost}
    return json.dump(r, _sys.stdout)


if __name__ == "__main__":
    run_main(main)
