#!/usr/bin/env python3
"""After-r14x3 fixes, each tested through a REAL Irrigator mini-plan (the lead, 2026-11-23: r14x3-1 computed the right
frame verdicts and still failed Irrigator's output contract, which adapter-only tests can't see).

1. hydrogen_frames returns exactly the fields build_round declares (and the old 4-field contract would reject it);
2. d_walls refreshes its kept v3c copy when the abs check ran again (a retry sees re-keyed reasons);
3. run_job keeps stderr out of the result file;
4. code_verify accepts a lead-recorded patch (PATCH_<n>.json) and nothing else.
No model is called: a tripwire agy is first on PATH and the test fails if it is ever run.
run: python3 -I pipeline/tools/test_irrigator_contracts.py      (needs the irrigator CLI)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE = os.path.dirname(HERE)
ADAPTERS = os.path.join(PIPE, "adapters")
sys.path.insert(0, PIPE)
import build_round as BR   # noqa: E402

TMP = tempfile.mkdtemp(prefix="irr_contract_")
ROOT, RUNS, BIN = (os.path.join(TMP, d) for d in ("root", "runs", "bin"))
for d in (ROOT, RUNS, BIN):
    os.makedirs(d)
TRIP = os.path.join(TMP, "agy_calls.log")
with open(os.path.join(BIN, "agy"), "w") as f:
    f.write(f"#!/bin/sh\necho \"$@\" >> '{TRIP}'\nexit 1\n")
os.chmod(os.path.join(BIN, "agy"), 0o755)
ENV = {**os.environ, "NYC_ROOT": ROOT, "PE_ROOT": "", "PIPELINE_RUNS": RUNS, "PATH": BIN + os.pathsep + os.environ["PATH"]}
H = lambda b: hashlib.sha256(b).hexdigest()


def plan(run, tasks, extra_providers=None):
    prov = {p: {"command": [BR.PY, "-I", os.path.join(ADAPTERS, p + ".py")], "env": BR.ENV}
            for p in ("highway", "codepin", "round_live")}
    prov.update(extra_providers or {})
    p = {"api_version": "swarm/v1", "name": "contract test", "max_tasks": len(tasks) + 2, "providers": prov, "tasks": tasks}
    json.dump(p, open(os.path.join(run, "plan.json"), "w"), indent=1)


def drive(run, ids, first=True, tries=12):
    """apply (or resume), then resume until every id in ids is verified or settled-failed; returns the tasks."""
    env = {**ENV, "PIPELINE_OUT": run}
    cmd = (["irrigator", "apply", os.path.join(run, "plan.json"), "--run", os.path.join(run, "run"), "--backend", "mock",
            "--workers", "2"] if first else ["irrigator", "resume", "--workers", "2", os.path.join(run, "run")])
    for _ in range(tries):
        subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
        t = json.load(open(os.path.join(run, "run", "state.json")))["tasks"]
        if all(t[i]["status"] == "verified" or (t[i]["status"] in ("failed", "uncertain")
                                                 and "job running" not in (t[i].get("error") or "")) for i in ids):
            return t
        for i in ids:   # like the supervisor: a step whose job is still running is retried, then resumed
            if t[i]["status"] == "failed" and "job running" in (t[i].get("error") or ""):
                subprocess.run(["irrigator", "retry", os.path.join(run, "run"), i], env=env, capture_output=True)
        cmd = ["irrigator", "resume", "--workers", "2", os.path.join(run, "run")]
        time.sleep(2)
    return json.load(open(os.path.join(run, "run", "state.json")))["tasks"]


def newrun(name, cfg):
    run = os.path.join(RUNS, name)
    os.makedirs(run)
    json.dump(cfg, open(os.path.join(run, "round.json"), "w"))
    return run


# ---------- 1. hydrogen_frames' contract ----------

SITES = ["edge_a_drive", "wall_b", "void_c"]


def frames_fixture(run):
    rel = "highways/work/census/frames/fdr_" + os.path.basename(run)
    d = os.path.join(ROOT, rel)
    os.makedirs(d)
    served, rows = {}, []
    for s in SITES:
        for i in (1, 2):
            b = f"{s} {i}".encode()
            open(os.path.join(d, f"{s}__{i}.jpg"), "wb").write(b)
            served[f"{s}__{i}.jpg"] = {"sha256": H(b)}
        rows.append({"pose": s, "sun_set": True, "noise_a_pct": 0.0, "noise_b_pct": 0.0, "g0_pixel_diff_pct": 0.0,
                     "mask_coverage_pct": 1.0})
    json.dump({"files": served}, open(os.path.join(d, "SERVED.json"), "w"))
    json.dump({"pairs": rows}, open(os.path.join(d, "guards_frames.json"), "w"))
    t = time.time() + 5
    open(os.path.join(d, "READY"), "w").close()
    os.utime(os.path.join(d, "READY"), (t, t))
    verdicts = {"edge_a_drive": "same", "wall_b": "mixed", "void_c": "same"}
    for s in SITES:   # finished Hydrogen reviews, as reused from an earlier run: no agy call is needed
        tag = "page_" + s + "_" + H(b"".join(open(os.path.join(d, f"{s}__{i}.jpg"), "rb").read() for i in (1, 2)))[:10]
        j, w = os.path.join(run, "jobs", "hydrogen_" + tag), os.path.join(run, "hydrogen", tag)
        os.makedirs(j)
        os.makedirs(w)
        json.dump({"pid": 1, "cmd": ["agy"], "cwd": w, "log": "x", "started": time.time() - 30}, open(os.path.join(j, "job.json"), "w"))
        open(os.path.join(j, "rc"), "w").write("0")
        resp = "Review.\n```json\n" + json.dumps({"fixed_vs_first": [], "new_vs_first": ["x"] if s == "wall_b" else [],
                                                    "verdict": verdicts[s]}) + "\n```"
        json.dump({"conversation_id": "c", "status": "SUCCESS", "response": resp}, open(os.path.join(w, "agy_stdout.json"), "w"))
    over = {"wall_b": {"verdict": "better", "by": "lead", "record": "LEAD_X.md",
                       "pair_sha256": [served["wall_b__1.jpg"]["sha256"], served["wall_b__2.jpg"]["sha256"]]}}
    return rel, over


def t_frames():
    cfg0 = {"round": "rct", "prev": "rprev", "kind": "highway_round", "mode": "live", "passes": [], "after_part_move": [],
            "p2_terv": "x", "input_build": "b"}
    for label, legacy in (("new", False), ("old", True)):
        run = newrun(f"frames_{label}", {})
        rel, over = frames_fixture(run)
        cfg = {**cfg0, "frames": {"folder": rel, "sites": [{"name": s} for s in SITES],
                                  "capture_guard": {"version": 3, "lead_rules": []}}, "frame_overrides": over}
        json.dump(cfg, open(os.path.join(run, "round.json"), "w"))
        nodes = {t["id"]: t for t in BR.highway_tasks(cfg)}
        fr, hf = dict(nodes["frames_ready"]), dict(nodes["hydrogen_frames"])
        fr.pop("depends_on", None)
        hf["depends_on"] = ["frames_ready"]
        if legacy:
            hf["requires"] = ["frames", "verdicts", "rows", "capture_guard"]   # r14x3-1's frozen contract
        plan(run, [fr, hf])
        t = drive(run, ["frames_ready", "hydrogen_frames"])
        h = t["hydrogen_frames"]
        if legacy:
            assert h["status"] != "verified" and "contract" in (h.get("error") or ""), h
            print("1a. the old 4-field contract rejects the new output, as in r14x3-1 (the test sees contracts): ok")
        else:
            assert h["status"] == "verified", h
            r = h["result"]
            assert set(r) == set(hf["requires"]), (sorted(r), hf["requires"])
            assert json.loads(r["verdicts"]) == {"same": 2, "better": 1} and json.loads(r["hydrogen_verdicts"]) == {"same": 2, "mixed": 1}
            assert json.loads(r["lead_overrides"])[0]["pose"] == "wall_b" and json.loads(r["override_errors"]) == []
            print("1b. hydrogen_frames verified through Irrigator with exactly build_round's declared fields: ok")
    assert not os.path.exists(TRIP), "agy was called"


# ---------- 2. d_walls keep refresh + 3. run_job stderr ----------

FAKE_WALLS = r'''import json, os, sys
rc, ref = sys.argv[1], sys.argv[2]
r = os.path.basename(rc)
u = int(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "unreasoned.txt")).read())
print("progress noise on stderr", file=sys.stderr)
if ref.endswith("empty_ref"):
    out, d = f"wall_near_lane_{r}_abs.json", {"on_road_m": 359.6, "on_road_triangles": 126, "on_road_unreasoned_triangles": u}
else:
    out, d = f"wall_near_lane_{r}.json", {"on_road_m": 0.0}
json.dump(d, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), out), "w"))
print(json.dumps(d))
'''
FAKE_V3A = r'''import json, os, sys
r = os.path.basename(sys.argv[1])
json.dump({"on_road_m": 1305.6, "on_road_triangles": 3168}, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
          f"wall_near_lane_{r}_abs.json"), "w"))
'''


def t_walls():
    p2 = os.path.join(ROOT, "highways/work/p2net")
    os.makedirs(os.path.join(p2, "final_fdr"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, ".venv/bin"), exist_ok=True)
    if not os.path.exists(os.path.join(ROOT, ".venv/bin/python")):
        os.symlink(sys.executable, os.path.join(ROOT, ".venv/bin/python"))
    open(os.path.join(p2, "wall_near_lane.py"), "w").write(FAKE_WALLS)
    open(os.path.join(p2, "wall_v3a.py"), "w").write(FAKE_V3A)
    open(os.path.join(p2, "unreasoned.txt"), "w").write("1")
    cfg = {"round": "rwt", "prev": "rprev", "kind": "highway_round", "mode": "live", "passes": [], "after_part_move": [],
           "p2_terv": "x", "input_build": "b", "walls_report_also": "work/p2net/wall_v3a.py",
           "walls_abs": {"metric": "m", "max_m": 1675.0, "unreasoned_gate": True}}
    run = newrun("walls", cfg)
    node = dict({t["id"]: t for t in BR.highway_tasks(cfg)}["d_walls"])
    node.pop("depends_on", None)
    plan(run, [node])
    t = drive(run, ["d_walls"])
    assert t["d_walls"]["status"] != "verified" and "unreasoned 1" in t["d_walls"]["result"]["detail"], t["d_walls"]
    # macmini re-keys the reasons; the policy-3 retry sets the check's jobs aside and runs it again
    open(os.path.join(p2, "unreasoned.txt"), "w").write("0")
    time.sleep(1.1)   # a newer mtime for the abs output than the kept copy
    for j in os.listdir(os.path.join(run, "jobs")):
        if j.startswith("d_walls"):
            os.rename(os.path.join(run, "jobs", j), os.path.join(run, "jobs", j + ".set_aside"))
    subprocess.run(["irrigator", "retry", os.path.join(run, "run"), "d_walls"], env={**ENV, "PIPELINE_OUT": run},
                   capture_output=True)
    t = drive(run, ["d_walls"], first=False)
    assert t["d_walls"]["status"] == "verified" and "unreasoned 0" in t["d_walls"]["result"]["detail"], t["d_walls"]
    assert "on v3" in t["d_walls"]["result"]["detail"] and "1305.6" in t["d_walls"]["result"]["detail"]
    print("2. d_walls: a retry after re-keyed reasons reads the fresh abs output (kept copy refreshed): ok")
    log = os.path.join(run, "d_walls_abs.log")
    json.loads(open(log).read())   # the result file holds only the script's stdout
    assert "progress noise" in open(log + ".stderr").read()
    print("3. run_job: stderr in <log>.stderr, the result file stays pure stdout: ok")


# ---------- 4. code_verify accepts a lead-recorded patch, and nothing else ----------

MUTATE = r'''import json, os, sys
req = json.load(sys.stdin)
out = lambda o: json.dump({"protocol": "swarm.worker/v1", "request_id": req["request_id"], "outcome": "settled",
                           "output": o, "cost_usd": 0}, sys.stdout)
if "valid" in (req.get("kind") or ""):
    json.dump({"protocol": "swarm.worker/v1", "request_id": req["request_id"], "passed": True, "cost_usd": 0}, sys.stdout)
    sys.exit()
run = os.environ["PIPELINE_OUT"]
mode = open(os.path.join(run, "mode.txt")).read().strip()
tgt = os.path.join(os.environ["NYC_ROOT"], "codetree", "check.py")
old = __import__("hashlib").sha256(open(tgt, "rb").read()).hexdigest()
open(tgt, "a").write("# patched\n")
new = __import__("hashlib").sha256(open(tgt, "rb").read()).hexdigest()
if mode != "none":
    open(os.path.join(run, "LEAD_PATCH_1.md"), "w").write("lead approves\n")
    rec = {"by": "lead", "record": "LEAD_PATCH_1.md", "reason": "fix a parser", "test_copy": "scratch/test1",
           "loosens_gate": False, "changes": [{"path": "codetree/check.py", "old_sha256": old if mode == "good" else "0" * 64,
                                               "new_sha256": new}]}
    json.dump(rec, open(os.path.join(run, "PATCH_1.json"), "w"))
out({"done": "yes"})
'''


def t_patch():
    for mode, want in (("none", False), ("good", True), ("bad_old", False)):
        tree = os.path.join(ROOT, "codetree")
        shutil.rmtree(tree, ignore_errors=True)
        os.makedirs(tree)
        open(os.path.join(tree, "check.py"), "w").write("print('check')\n")
        run = newrun(f"patch_{mode}", {"round": f"rp_{mode}", "kind": "highway_round", "code_roots": ["codetree"]})
        open(os.path.join(run, "mode.txt"), "w").write(mode)
        mut = os.path.join(TMP, "mutate.py")
        open(mut, "w").write(MUTATE)
        cp = BR.node("code_pin", "codepin", ["pass", "detail"], validated=True)
        mu = {"id": "mutate", "provider": "mut", "prompt": "mutate", "requires": ["done"], "depends_on": ["code_pin"]}
        cv = BR.node("code_verify", "codepin", ["pass", "detail"], after=["mutate"], validated=True)
        plan(run, [cp, mu, cv], {"mut": {"command": [sys.executable, "-I", mut], "env": BR.ENV}})
        t = drive(run, ["code_pin", "mutate", "code_verify"])
        v = t["code_verify"]
        assert (v["status"] == "verified") == want, (mode, v)
        det = (v.get("result") or {}).get("detail", "")
        if mode == "good":
            assert "lead patches accepted: PATCH_1.json" in det and "check.py" in det, det
        if mode == "bad_old":
            assert "patch records refused" in det and "old_sha256" in det, det
        print(f"4{'abc'[['none', 'good', 'bad_old'].index(mode)]}. code_verify, patch record {mode}: "
              f"{'verified' if want else 'fails the round'}: ok")


if __name__ == "__main__":
    try:
        t_frames()
        t_walls()
        t_patch()
        assert not os.path.exists(TRIP), "agy was called: " + open(TRIP).read()
        print("all Irrigator contract tests pass (0 agy calls)")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
