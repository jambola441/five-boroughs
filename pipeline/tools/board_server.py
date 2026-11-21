#!/usr/bin/env python3
"""Live Round Board: a local web page that shows every round run as it moves, straight from Irrigator's state.

usage: PIPELINE_RUNS=<runs folder> python3 -I pipeline/tools/board_server.py [port, default 8790]
Serves on 127.0.0.1 only. Read-only: it reads <run>/run/state.json, <run>/round.json and the run's own logs,
and never writes, retries or runs anything.
"""
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from board_data import detail_of, state_of, KIND  # noqa: E402

RUNS = os.environ.get("PIPELINE_RUNS") or sys.exit("set PIPELINE_RUNS")
PAGE = os.path.join(HERE, "board_page.html")
PATH_RX = re.compile(r"(?:/[^/\s,'\"]+){2,}")

# Plain-English names for step ids; anything not listed is shown from its id.
LABELS = {
    "t_build": "Build terrain", "t_checks": "Terrain checks", "terrain_accept": "Terrain accept (Gemini)",
    "t_freeze": "Freeze terrain", "qa_disk": "Disk check", "qa_round_gate": "Drive-test QA gate",
    "gate_pins": "Gate: pins", "gate_a": "Gate (a): free ends", "gate_b": "Gate (b): deck gap",
    "gate_c": "Gate (c): lost contact", "gate_ramps": "Gate: ramps", "gate_city": "Gate: city scan",
    "gate_walls": "Gate: walls", "gate_flight": "Gate: flight", "frames_ready": "Frames from page editor",
    "bundle": "Bundle vs live", "qa_accept": "QA accept (Gemini)", "lead_gate": "Lead approval",
    "register_update": "Register row", "pin_inputs": "Pin inputs", "terrain_checks": "Terrain checks",
    "disk_guard": "Disk check", "drive_gate": "Drive gate", "city_census": "City census",
    "explore_gate": "Exploration gate", "explore_accept": "Exploration accept",
    "h_guard": "Guard (write-once, disk)", "h_assemble": "Assemble build", "h_part_move": "Part move",
    "h_cbe_copy": "CBE copy", "d_floor_fix_guard": "Stop: floor-fix guard", "d_tile_inventory": "Stop: tile inventory",
    "d_no_surface": "Stop: no surface on path", "d_unsupported": "Stop: unsupported", "d_underlay": "Stop: underlay",
    "d_void": "Stop: see-through voids", "d_walls": "Stop: walls on road", "d_columns": "Stop: columns",
    "d_crossing": "Stop: crossings", "d_listed": "Reported checks", "highway_accept": "Highway accept (Gemini)",
    "p_disk": "Disk check", "p_drape": "Drape variant", "p_drape_base": "Drape base", "p_lift_gate": "Lift gate + city",
    "p_lift_base": "Lift gate (base)", "p_newlist": "New / gone steps", "p_deck_under": "Deck gap",
    "p_deck_base": "Deck gap (base)", "p_ramps": "Ramp census", "p_ramps_base": "Ramp census (base)",
    "p_census": "Polygon census", "p_census_base": "Polygon census (base)", "g_newlist": "Gate: new steps",
    "g_deck_under": "Gate: deck gap", "g_ramps": "Gate: ramps", "g_census": "Gate: tents", "g_lift": "Gate: lift / city",
    "hydrogen_frames": "Frame review (Gemini)", "page_accept": "Page accept (Gemini)",
}
WAITS = {"frames_ready": "the page editor's frames", "lead_gate": "the lead's approval"}


def label(tid):
    if tid in LABELS: return LABELS[tid]
    m = re.match(r"h_pass_(\d+)", tid)
    if m: return f"Pass {int(m.group(1)) + 1}"
    m = re.match(r"h_after_(\d+)", tid)
    if m: return "After part move"
    if tid.startswith("p_trace"): return "Trace " + tid[len("p_trace_"):].replace("_", " ")
    if tid.startswith("p_lidar"): return "LiDAR check " + tid[len("p_lidar_"):].replace("_", " ")
    if tid.startswith("p_newlist"): return "New steps " + tid[len("p_newlist_"):].replace("_", " ")
    if tid.startswith("hydrogen_"): return "Frame review " + tid[len("hydrogen_"):]
    return tid.replace("_", " ")


def scrub(s):
    return PATH_RX.sub("…", s or "")


def run_view(name):
    d = os.path.join(RUNS, name)
    st = json.load(open(os.path.join(d, "run", "state.json")))
    cfg = json.load(open(os.path.join(d, "round.json")))
    steps = []
    for t in st["plan"]["tasks"]:
        x = st["tasks"][t["id"]]
        s = state_of(x)
        err = x.get("error") or ""
        if "job running" in err: s = "running"
        if s == "waiting" and t["id"] == "lead_gate": s = "waiting"
        job = os.path.join(d, "jobs")
        r = x.get("result") or {}
        prompt = t.get("prompt", "")
        steps.append({"id": t["id"], "label": label(t["id"]), "state": s, "deps": t.get("depends_on") or [],
                      "detail": scrub(detail_of(x)), "error": scrub(err)[:400], "attempts": x.get("attempt", 0),
                      "note": prompt if prompt.startswith(("post-pass", "after part")) else "",
                      "result": {k: scrub(str(v))[:1500] for k, v in r.items()
                                 if k not in ("out", "flags", "gate_log", "gate_dir", "request", "rows")}})
    by = {}
    for s in steps: by[s["state"]] = by.get(s["state"], 0) + 1
    stop = next((s for s in steps if s["state"] in ("failed", "rejected", "uncertain")), None)
    wait = [s for s in steps if s["state"] == "waiting"]
    running = [s for s in steps if s["state"] == "running"]
    if stop:
        headline = ("stopped", f"Stopped at {stop['label']}: {stop['detail'] or stop['error']}")
    elif running:
        headline = ("running", "Running: " + ", ".join(s["label"] for s in running))
    elif wait:
        headline = ("waiting", "Waiting on " + ", ".join(WAITS.get(s["id"], s["label"]) for s in wait))
    elif by.get("done", 0) == len(steps):
        headline = ("done", "Finished")
    else:
        headline = ("pending", "Not started")
    return {"name": name, "round": cfg.get("round", name), "kind": KIND.get(cfg.get("kind"), "Shadow round"),
            "mode": cfg.get("mode", "live"), "updated": os.path.getmtime(os.path.join(d, "run", "state.json")),
            "started": os.path.getmtime(os.path.join(d, "round.json")),
            "counts": by, "total": len(steps), "status": headline[0], "headline": scrub(headline[1])[:400],
            "steps": steps}


def all_runs():
    out = []
    for n in os.listdir(RUNS):
        if os.path.exists(os.path.join(RUNS, n, "run", "state.json")) and os.path.exists(os.path.join(RUNS, n, "round.json")):
            try:
                out.append(run_view(n))
            except Exception as e:   # a run mid-write: show it next poll
                out.append({"name": n, "round": n, "status": "pending", "headline": f"unreadable right now ({type(e).__name__})",
                            "steps": [], "counts": {}, "total": 0, "updated": 0, "kind": "", "mode": ""})
    out.sort(key=lambda r: -r["updated"])
    return out


def log_tail(name, step):
    d = os.path.join(RUNS, os.path.basename(name))
    cands = [os.path.join(d, step + ".log")] + [os.path.join(d, f) for f in sorted(os.listdir(d))
                                                if f.startswith(step) and f.endswith(".log")]
    job = os.path.join(d, "jobs", step, "job.json")
    if os.path.exists(job):
        cands.insert(0, os.path.join(d, json.load(open(job)).get("log", "")))
    for p in cands:
        p = os.path.realpath(p)
        if p.startswith(os.path.realpath(d) + os.sep) and os.path.isfile(p):
            with open(p, errors="replace") as f:
                lines = f.read().splitlines()[-25:]
            return scrub("\n".join(lines))
    return ""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self.send(200, open(PAGE, encoding="utf-8").read(), "text/html; charset=utf-8")
        if u.path == "/api/runs":
            return self.send(200, json.dumps({"at": time.time(), "runs": all_runs()}), "application/json")
        if u.path == "/api/log":
            q = parse_qs(u.query)
            return self.send(200, json.dumps({"text": log_tail(q.get("run", [""])[0], q.get("step", [""])[0])}),
                             "application/json")
        self.send(404, "not found", "text/plain")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    print(f"Round board on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
