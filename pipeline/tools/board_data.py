#!/usr/bin/env python3
"""Rows for the Round Graph Board: one row per step of each listed run, read from Irrigator's own state.

usage: board_data.py RUNS_DIR OUT.json run_name [run_name ...]
Reads RUNS_DIR/<run>/run/state.json and RUNS_DIR/<run>/round.json (read-only). Writes {rows, meta}.
No local paths or file contents go into the output: only round names, step names, states and the verdict text
the steps themselves produced.
"""
import datetime
import json
import os
import re
import sys

WAIT = re.compile(r"awaiting|plan-only|has not set|not written yet|GB free|need \d+ GB|job running|needs \w+ first|paused", re.I)
KIND = {"terrain_round_live": "Terrain + QA round", "page_rule_explore": "Exploration round",
        "highway_round": "Highway round", "page_round": "Page round", None: "Shadow round"}


def state_of(t):
    s, err = t.get("status"), t.get("error") or ""
    if s == "verified": return "done"
    if s == "failed" and WAIT.search(err): return "waiting"
    if s == "failed" and (t.get("result") or {}).get("decision") in ("reject", "stop"): return "rejected"
    if s == "failed": return "failed"
    if s in ("running", "pending", "uncertain", "cancelled"): return s
    return s or "pending"


def detail_of(t):
    r = t.get("result") or {}
    if r.get("decision") and r.get("reasons"):
        try:
            first = json.loads(r["reasons"])[0]
        except Exception:
            first = r["reasons"]
        return f"{r['decision']}: {first}"[:300]
    for k in ("detail", "line", "decision"):
        if r.get(k): return str(r[k])[:300]
    if r.get("free_ends"): return f"free ends {r['free_ends']}, crest {r.get('crest_8pct')}/{r.get('crest_12pct')}"
    if r.get("terrain_sha"): return "manifest verified, sha " + r["terrain_sha"][:8]
    err = re.sub(r"(?:/[^/\s]+){2,}", "<path>", t.get("error") or "")   # scrub any absolute path
    return err[:300]


def main():
    runs_dir, out, names = sys.argv[1], sys.argv[2], sys.argv[3:]
    rows = []
    for order, name in enumerate(names):
        st = json.load(open(os.path.join(runs_dir, name, "run", "state.json")))
        cfg = json.load(open(os.path.join(runs_dir, name, "round.json")))
        plan_ids = [t["id"] for t in st["plan"]["tasks"]]
        for i, tid in enumerate(plan_ids):
            t = st["tasks"][tid]
            rows.append({"run": name, "run_order": order, "round": cfg.get("round", name),
                         "kind": KIND.get(cfg.get("kind"), "Shadow round"), "mode": cfg.get("mode", "live"),
                         "step": tid, "step_order": i, "state": state_of(t), "attempts": t.get("attempt", 0),
                         "deps": ",".join(t["spec"].get("depends_on") or []), "detail": detail_of(t)})
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    json.dump({"rows": rows, "meta": [{"label": "Built", "value": stamp},
                                      {"label": "From", "value": "Irrigator run state (state.json) of each listed run"},
                                      {"label": "Runs", "value": ", ".join(names)}]},
              open(out, "w"), indent=0)
    print(len(rows), "rows from", len(names), "runs")


if __name__ == "__main__":
    main()
