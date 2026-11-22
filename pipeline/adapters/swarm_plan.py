#!/usr/bin/env python3
"""Floor-plan stage of a research swarm (research_swarm with "stage": "plan"; the owner's floor-plan idea, 2026-11-22).
A new file beside swarm.py, which it imports unchanged (swarm.py is pinned by running rounds). Per item:

  <item>__main    the plan turn: the main agent writes plans/plan_cx_<id>.json (and may correct its card), returns PLAN_SCHEMA
  <item>__fix     the repair pass: the round's check (check_plan.py) runs; if the plan is malformed or has FAIL items, the
                  agent gets the findings and the fix_brief once; otherwise it doesn't run
  <item>__check   the round's 'after' commands in order (re-check, render, card gate): passes when every one exits 0
  swarm_report    per-item plan summary, FAIL/FLAG/sheet counts, minutes and tokens, plus the round's 'score' output

Placeholders in check / after / score commands: {plan} = the item's plan (writes[0]), {card} = its card (writes[1]),
{id} and the item's vars, {NYC_ROOT}. Findings never block: check_plan.py exits 1 only for a malformed plan.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import json
import os
import subprocess

import agy_cleanup
import prestep
import swarm as S
from common import OUT, deps, load_request, project, respond, round_config, run_main, validate

PLAN_SCHEMA = {"type": "object", "properties": {
    "plan": {"type": "string"}, "levels": {"type": "string"}, "stairs": {"type": "string"},
    "fare_lines": {"type": "string"}, "corrections": {"type": "string"}, "doubts": {"type": "string"},
    "minutes": {"type": "string"}, "repairs": {"type": "string"}},
    "required": ["plan", "levels", "stairs", "fare_lines", "corrections", "doubts"]}


def paths(cfg, item):
    f = [os.path.join(S.root(cfg), x) for x in S.item_files(cfg, item)]
    return f[0], (f[1] if len(f) > 1 else "")


def cmd_of(cfg, item, cmd):
    plan, card = paths(cfg, item)
    cmd = [project(x) if x.startswith(("pe:", "runs:")) else x for x in cmd]
    return [S.fill(cfg, item, c).replace("{plan}", plan).replace("{card}", card) for c in cmd]


def run(cfg, item, cmd):
    p = subprocess.run(cmd_of(cfg, item, cmd), cwd=S.root(cfg), capture_output=True, text=True, timeout=100)
    return p.returncode, (p.stdout.strip() or p.stderr.strip())


def findings(cfg, item):
    """check_plan.py's file next to the plan: plans/plan_check_cx_<id>.json (None if it hasn't run)."""
    plan, _ = paths(cfg, item)
    f = os.path.join(os.path.dirname(plan), f"plan_check_cx_{item}.json")
    try:
        return json.load(open(f))
    except (OSError, ValueError):
        return None


def settled(req, resp, cost):
    json.dump({"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled",
               "output": S.nonempty(resp), "cost_usd": cost}, _sys.stdout)


def model(cfg):
    return cfg["main"].get("model", "claude-opus-5-5")


def files_line(cfg, item):
    agy = cfg.get("engine") == "agy"
    return ("Write only these files (relative to your working folder): "
            + ", ".join(os.path.basename(f) if agy else f for f in S.item_files(cfg, item))
            + (" (copies, already in your working folder: edit them in place; the graph copies them back)" if agy else "") + ".")


def main():
    req = load_request()
    tid = req["task"]["id"]
    cfg = round_config()
    agy_cleanup.wrap(S)   # each agy call's own ~/.gemini brain folder goes after its step (owner's OK, 2026-11-22)
    if cfg.get("allow_google_imagery"):   # owner, 2026-11-22: "you can now use street view to gather photos"
        S.DENY_HOSTS[:] = []   # agy_settings reads it per call; swarm.py itself stays unchanged (pinned by r14x2-1)
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or req.get("output") or {}
        if tid.endswith("__check"):
            return validate(req, c.get("pass") == "true")
        return validate(req, True)
    d = deps(req)
    item, _, part = tid.partition("__")
    if part in ("main", "fix"):
        prestep.wrap(S, cfg, item, part)   # the round's pre-step, if any, prepares this step's working folder

    if tid == "swarm_report":
        rows = []
        for lane_i, lane in enumerate(S.lanes(cfg)):
            for it in lane:
                m, fx = d.get(f"{it}__main", {}), d.get(f"{it}__fix", {})
                fnd = findings(cfg, it) or {}
                plan, _ = paths(cfg, it)
                svg = os.path.splitext(plan)[0] + ".svg"
                rows.append({"item": it, "lane": lane_i + 1, "check_step": S.check_state(it)[:300],
                             "summary": fnd.get("summary", {}), "malformed": fnd.get("malformed", []),
                             "svg": svg if os.path.exists(svg) else "",
                             "main_minutes": float(m.get("minutes_wall", 0) or 0), "main_tokens": int(m.get("tokens", 0) or 0),
                             "fix_ran": fx.get("ran", ""), "fix_minutes": float(fx.get("minutes_wall", 0) or 0),
                             "fix_tokens": int(fx.get("tokens", 0) or 0), "fails_before_repair": fx.get("fails_before", ""),
                             "corrections": m.get("corrections", ""), "doubts": m.get("doubts", "")})
        tot = lambda k: sum(int((r["summary"] or {}).get(k, 0) or 0) for r in rows)
        score = ""
        if cfg.get("score"):
            try:
                rc, score = run(cfg, rows[0]["item"] if rows else "", cfg["score"])
            except (subprocess.TimeoutExpired, OSError) as e:
                score = f"score did not run: {e}"
            open(os.path.join(OUT, "score.txt"), "w").write(score + "\n")
        rep = {"round": cfg["round"], "items": len(rows), "fail_total": tot("fail"), "flag_total": tot("flag"),
               "sheet_items_total": tot("sheet_items"),
               "tokens_total": sum(r["main_tokens"] + r["fix_tokens"] for r in rows), "score": score, "rows": rows}
        for p in (os.path.join(OUT, "swarm_report.json"), os.path.join(S.root(cfg), "plan_cal_report.json")):
            json.dump(rep, open(p, "w"), indent=1)
        return respond(req, output={"items": len(rows), "plans_checked": sum(1 for r in rows if r["summary"]),
                                    "fail_total": rep["fail_total"], "flag_total": rep["flag_total"],
                                    "sheet_items_total": rep["sheet_items_total"], "tokens_total": rep["tokens_total"],
                                    "report": os.path.join(OUT, "swarm_report.json")})

    if part == "check":   # the after-commands in order: re-check, render, card gate
        lines, ok = [], True
        for cmd in cfg.get("after") or [cfg["check"]]:
            rc, out = run(cfg, item, cmd)
            ok = ok and rc == 0
            lines.append(("ok " if rc == 0 else f"rc={rc} ") + out.splitlines()[0][:300] if out else f"rc={rc}")
        prestep.done(cfg, item)
        return respond(req, output={"pass": str(ok).lower(), "detail": ("PASS " if ok else "FAIL ") + " | ".join(lines)[:900]})

    if part == "main":
        plan, _ = paths(cfg, item)
        os.makedirs(os.path.dirname(plan), exist_ok=True)
        if not os.path.exists(plan):
            open(plan, "w").write("{}\n")   # the brief's "empty at first"; check_plan.py calls it malformed until written
        ctx = [f"You are planning item {item}. " + files_line(cfg, item)]
        if cfg.get("conventions"):
            ctx.append(f"Read {os.path.join(S.root(cfg), S.item_ref(cfg, item, 'conventions', cfg['conventions']))} first: "
                       "the plan schema, read-only.")
        prev = S.prev_item(cfg, item)
        if prev:
            pm = d.get(f"{prev}__main", {})
            ctx.append(f"The previous item in your lane was {prev} (its plan: {paths(cfg, prev)[0]}); it reported: "
                       f"{pm.get('summary', '')[:500]}; doubts: {pm.get('doubts', '')[:300]}. Reuse what you learned.")
        if cfg["main"].get("output_note"):
            ctx.append(S.fill(cfg, item, cfg["main"]["output_note"]))
        prompt = S.fill(cfg, item, S.brief(cfg, S.item_ref(cfg, item, "brief_main", cfg["main"]["brief"]))) + "\n\n" + "\n".join(ctx)
        out, cost, mins, denied = S.agent(f"{item}__main", prompt, cfg, model(cfg), cfg["main"].get("budget_usd", 5),
                                          PLAN_SCHEMA, writes=S.item_files(cfg, item))
        resp = {"plan": out["plan"], "summary": f"levels {out['levels']}; stairs {out['stairs']}; fare lines {out['fare_lines']}",
                "corrections": out.get("corrections", ""), "doubts": out.get("doubts", ""), "minutes_wall": mins,
                "tokens": S.TOKENS.get(f"{item}__main", 0)}
        return settled(req, resp, cost)

    if part == "fix":   # the repair pass: check_plan.py's FAIL items (or malformed errors) back to the agent, once
        rc, line = run(cfg, item, cfg["check"])
        fnd = findings(cfg, item) or {}
        fails = [i for i in fnd.get("items", []) if i.get("severity") == "FAIL"]
        base = {"check_before": line[:300], "fails_before": len(fails) + (len(fnd.get("malformed", [])) or int(rc != 0))}
        if rc == 0 and not fails:
            return respond(req, output={**base, "ran": "false", "repairs": "none", "minutes_wall": 0, "tokens": 0})
        shown = {"malformed": fnd.get("malformed", []) or ([line] if rc else []), "summary": fnd.get("summary", {}), "FAIL": fails}
        prompt = (S.fill(cfg, item, S.brief(cfg, cfg.get("fix_brief") or cfg["main"]["brief"])) + "\n\nREPAIR PASS for item "
                  + item + ". " + files_line(cfg, item) + "\n" + S.fill(cfg, item, cfg["main"].get("output_note") or "")
                  + f"\ncheck_plan.py said: {line[:600]}\nIts findings (plans/plan_check_cx_{item}.json, FAIL items and "
                  "malformed errors only; FLAG items go to the owner as they are):\n"
                  + json.dumps(shown, ensure_ascii=False, indent=1)[:9000]
                  + "\nEdit the plan (and the card, with corrections) in your working folder, then return the turn output.")
        out, cost, mins, _ = S.agent(f"{item}__fix", prompt, cfg, model(cfg), cfg["main"].get("budget_usd", 5),
                                     PLAN_SCHEMA, writes=S.item_files(cfg, item))
        return settled(req, {**base, "ran": "true", "repairs": out.get("repairs", ""), "minutes_wall": mins,
                             "tokens": S.TOKENS.get(f"{item}__fix", 0)}, cost)

    raise RuntimeError(f"swarm_plan: no part '{part}' (the plan stage has no helpers)")


if __name__ == "__main__":
    run_main(main)
