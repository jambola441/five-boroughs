#!/usr/bin/env python3
"""POST_R14X2 items 3, 4, 5, 6, 9 and the lead's frame_overrides, on fake frames folders (no Hydrogen, no Gemini).

run: python3 -I pipeline/tools/test_frames_guard.py
     (with NYC_ROOT set, G0 v3 is also run on r14x2's real guards_frames.json: 13/13 must pass with both lead rules
     granted, and go to the lead with none granted)
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTERS = os.path.join(HERE, "..", "adapters")
TMP = tempfile.mkdtemp(prefix="frames_test_")
REAL_ROOT = os.environ.get("NYC_ROOT", "")
os.environ.update(NYC_ROOT=TMP, PIPELINE_OUT=os.path.join(TMP, "out"))
os.makedirs(os.environ["PIPELINE_OUT"])
sys.path.insert(0, ADAPTERS)
import frames_guard as FG   # noqa: E402
import g0_v2   # noqa: E402
import g0_v3   # noqa: E402
import page as P   # noqa: E402
import highway as H   # noqa: E402
import round_live as RL   # noqa: E402

OUT = os.environ["PIPELINE_OUT"]
SITES = ["edge_a_drive", "wall_b", "void_c"]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def make_folder(name, sites=SITES, served=True, ready=True):
    d = os.path.join(TMP, name)
    os.makedirs(d)
    files = {}
    for s in sites:
        for i in (1, 2):
            f = f"{s}__{i}.jpg"
            b = f"{s} panel {i}".encode()
            open(os.path.join(d, f), "wb").write(b)
            files[f] = {"sha256": sha(b), "panel": "prev" if i == 1 else "this"}
    if served:
        json.dump({"round": "fake", "files": files}, open(os.path.join(d, "SERVED.json"), "w"))
    if ready:
        t = time.time() + 5
        open(os.path.join(d, "READY"), "w").close()
        os.utime(os.path.join(d, "READY"), (t, t))
    return d


def guards(d, rows):
    json.dump({"pairs": rows}, open(os.path.join(d, "guards_frames.json"), "w"))


def row(pose, **kw):
    r = {"pose": pose, "sun_set": True, "noise_a_pct": 0.0, "noise_b_pct": 0.0, "g0_pixel_diff_pct": 0.01,
         "mask_coverage_pct": 1.0}
    r.update(kw)
    return r


def t_served():
    d = make_folder("served_ok")
    assert FG.served(d)[0], FG.served(d)
    d = make_folder("served_none", served=False)
    ok, why = FG.served(d)
    assert not ok and "no SERVED.json" in why
    d = make_folder("served_badsha")
    open(os.path.join(d, "wall_b__2.jpg"), "wb").write(b"overwritten by a stray tab")
    t = os.path.getmtime(os.path.join(d, "READY")) - 1
    os.utime(os.path.join(d, "wall_b__2.jpg"), (t, t))
    ok, why = FG.served(d)
    assert not ok and "sha mismatch" in why and "wall_b__2.jpg" in why, why
    d = make_folder("served_late")
    t = os.path.getmtime(os.path.join(d, "READY")) + 60
    os.utime(os.path.join(d, "void_c__1.jpg"), (t, t))
    ok, why = FG.served(d)
    assert not ok and "written after READY" in why, why
    d = make_folder("served_extra")
    open(os.path.join(d, "r14tB_wall_b.jpg"), "wb").write(b"x")
    ok, why = FG.served(d)
    assert not ok and "not in SERVED.json" in why, why
    os.remove(os.path.join(d, "wall_b__1.jpg"))
    ok, why = FG.served(d)
    assert not ok and "missing" in why, why
    print("6. SERVED.json: ok / none / sha mismatch / after READY / extra / missing -> re-shoot: ok")


def t_sites():
    d = make_folder("sites_bad", sites=SITES + ["r14tB_wall_b"])
    ok, why = FG.sites_match(d, SITES)
    assert not ok and "r14tB_wall_b" in why
    assert FG.sites_match(make_folder("sites_ok"), SITES)[0]
    cfg = {"frames": {"folder": "sites_bad", "panels": ["prev", "this"]}}
    try:
        P.hydrogen_frames(cfg, expect=SITES)
        raise AssertionError("hydrogen_frames must refuse a folder whose sites differ")
    except RuntimeError as e:
        assert "don't match the request's poses" in str(e) and "r14tB_wall_b" in str(e), e
    print("4. a folder whose sites aren't the request's poses is refused before any review: ok")


def t_rows():
    rows = [{"site": f"s{i}", "files": [f"s{i}__1.jpg", f"s{i}__2.jpg"], "verdict": "same", "fixed": [],
             "new": ["x" * 120], "seconds": 1.0} for i in range(92)]
    f = P.rows_field(rows)
    back = json.loads(f)   # r14x2-1 crashed here on a cut field
    assert len(f) <= 6000 or len(back) == 92 and set(back[0]) == {"site", "verdict"}, f[:200]
    assert json.loads(P.rows_field(rows[:3])) == rows[:3]
    print("3. rows stay valid JSON past 6,000 chars (short form; the full rows stay in the file): ok")


def t_site_of():
    names = ["edge_46723389_drive", "void_x-3_z15_drive", "void_x-3_z15", "void_x-3_z16", "void_x8_z6"]
    so = g0_v2.site_of
    assert so("void_x-3_z15_drive", names) == "void_x-3_z15_drive"
    assert so("void_x-3_z15", names) == "void_x-3_z15"
    assert "void_x-3_z15" not in {so(n, names) for n in names if n != "void_x-3_z15"}
    assert so("void_x-3_z15_air", names) is None
    assert so("x-3_z15", names) is None
    assert so("void_x8_z", names) == "void_x8_z6"
    print("9. site_of exact-match rule (g0_v2, in since 77c2e8b): all 6 staged cases: ok")


def t_g0_v3():
    hist = os.path.join(TMP, "hist.json")
    def ev(d, **kw):   # a fresh history per case (the 3-invalid cap is per pose across shoots)
        if os.path.exists(hist):
            os.remove(hist)
        return g0_v3.evaluate(d, SITES, 0.05, hist, **kw)
    d = make_folder("g0_ok")
    guards(d, [row(s) for s in SITES])
    assert ev(d)[0] == "pass", ev(d)
    d = make_folder("g0_nocov")
    guards(d, [row(s) for s in SITES[:2]] + [{k: v for k, v in row("void_c").items() if k != "mask_coverage_pct"}])
    st, why, _ = ev(d)
    assert st == "reshoot" and "no mask coverage" in why, why
    d = make_folder("g0_bigmask")
    guards(d, [row(s) for s in SITES[:2]] + [row("void_c", mask_coverage_pct=31.0)])
    st, why, _ = ev(d)
    assert st == "lead" and "mask covers 31.0 %" in why, why
    d = make_folder("g0_rule_not_granted")
    tm = row("void_c", lead_rule="twin_match", lead_record="LEAD_G0_DECISION.md",
             picked_tags={"A": "r14t", "A2": "r14tB", "B": "r14x2C", "B2": "r14x2D"})
    guards(d, [row(s) for s in SITES[:2]] + [tm])
    st, why, _ = ev(d)
    assert st == "lead" and "not granted" in why, why
    st, why, _ = ev(d, lead_rules=["twin_match"])
    assert st == "pass", why
    guards(d, [row(s) for s in SITES[:2]] + [{**tm, "g0_pixel_diff_pct": 0.4}])
    st, why, _ = ev(d, lead_rules=["twin_match"])
    assert st == "lead" and "no cross-round pair" in why, why   # twins clean, A vs B over: straight to the lead
    guards(d, [row(s) for s in SITES[:2]] + [{**tm, "noise_b_pct": 0.3}])
    st, why, _ = ev(d, lead_rules=["twin_match"])
    assert st == "reshoot", why
    guards(d, [row(s) for s in SITES[:2]] + [{**tm, "picked_tags": {"A": "r14t"}}])
    assert ev(d, lead_rules=["twin_match"])[0] == "reshoot"
    hw = row("edge_a_drive", lead_rule="hw_streaming", lead_record="LEAD_HYDROGEN_WORSE_DECISION.md",
             layers_A_vs_B_equal_excl_highways=True, hw_tris={"a": 1, "b": 2}, g0_pixel_diff_pct=0.02)
    d = make_folder("g0_hw")
    guards(d, [hw, row("wall_b"), row("void_c")])
    assert ev(d, lead_rules=["hw_streaming"])[0] == "pass"
    guards(d, [{**hw, "layers_A_vs_B_equal_excl_highways": False}, row("wall_b"), row("void_c")])
    assert ev(d, lead_rules=["hw_streaming"])[0] == "reshoot"
    guards(d, [{**hw, "whole_frame_pct": 0.3}, row("wall_b"), row("void_c")])
    assert ev(d, lead_rules=["hw_streaming"])[0] == "reshoot"
    guards(d, [row("edge_a_drive", lead_rule="made_up", lead_record="x"), row("wall_b"), row("void_c")])
    assert ev(d, lead_rules=["hw_streaming", "made_up"])[0] == "lead"
    d = make_folder("g0_nosun")
    guards(d, [row(s) for s in SITES[:2]] + [row("void_c", sun_set=False)])
    assert ev(d)[0] == "reshoot"
    msg = "5. G0 v3: mask coverage required, > 25 % -> lead; twin_match / hw_streaming only when granted; v2 rules kept: ok"
    if REAL_ROOT:
        real = os.path.join(REAL_ROOT, "highways/work/census/frames/fdr_r14x2")
        if os.path.exists(os.path.join(real, "guards_frames.json")):
            rd = os.path.join(TMP, "real_r14x2")
            os.makedirs(rd)
            shutil.copy(os.path.join(real, "guards_frames.json"), rd)
            names = [p["pose"] for p in json.load(open(os.path.join(rd, "guards_frames.json")))["pairs"]]
            h2 = os.path.join(TMP, "hist_real.json")
            st, why, rows = g0_v3.evaluate(rd, names, 0.05, h2, lead_rules=["twin_match", "hw_streaming"])
            assert st == "pass" and len(rows) == 13, why
            os.remove(h2)
            assert g0_v3.evaluate(rd, names, 0.05, h2)[0] == "lead"
            msg += " (and r14x2's real rows: 13/13 pass with both rules granted, lead without)"
    print(msg)


def t_overrides():
    d = make_folder("ov")
    rel = "ov"
    rows = [{"site": s, "files": [f"{s}__1.jpg", f"{s}__2.jpg"], "verdict": v, "fixed": [], "new": ["a tan wall"], "seconds": 1}
            for s, v in zip(SITES, ["same", "mixed", "worse"])]
    good = lambda s: [FG.sha256(os.path.join(d, f"{s}__{i}.jpg")) for i in (1, 2)]
    ov = {"wall_b": {"verdict": "better", "by": "lead", "record": "LEAD_X.md", "pair_sha256": good("wall_b")}}
    out, applied, errors = FG.apply_overrides(rows, d, ov)
    w = next(r for r in out if r["site"] == "wall_b")
    assert w["verdict"] == "better" and w["hydrogen"]["verdict"] == "mixed" and w["hydrogen"]["new"] == ["a tan wall"]
    assert w["override"].startswith("lead override") and applied[0]["kind"] == "lead override" and not errors
    assert rows[1]["verdict"] == "mixed", "the input rows are never changed"
    bad = {"wall_b": {**ov["wall_b"], "pair_sha256": [good("wall_b")[1], good("wall_b")[0]]}}   # swapped panels
    out, applied, errors = FG.apply_overrides(rows, d, bad)
    assert not applied and len(errors) == 1 and "doesn't match" in errors[0]
    assert next(r for r in out if r["site"] == "wall_b")["verdict"] == "mixed", "a mismatched override is ignored"
    for broken, needle in (({"by": "macmini"}, "by must be 'lead'"), ({"verdict": "fine"}, "verdict must be"),
                           ({"record": ""}, "no record"), ({"pair_sha256": good("wall_b")[:1]}, "has 1 shas"),
                           ({"pair_sha256": ["ABC"]}, "lowercase 64-hex")):
        _, a, e = FG.apply_overrides(rows, d, {"wall_b": {**ov["wall_b"], **broken}})
        assert not a and needle in e[0], (broken, e)
    _, a, e = FG.apply_overrides(rows, d, {"nowhere": ov["wall_b"]})
    assert not a and "no reviewed row" in e[0]
    print("frame_overrides: sha-matched override applies, Hydrogen's text kept; any mismatch ignored + error: ok")

    # through highway.frames_step: the review is faked, everything else is the real code path
    cfg = {"round": "fake", "prev": "prevr", "kind": "highway_round", "mode": "live",
           "frames": {"folder": rel, "sites": [{"name": s} for s in SITES],
                      "capture_guard": {"version": 3, "lead_rules": []}}}
    guards(d, [row(s) for s in SITES])

    def fake_review(c, expect=None):
        assert expect == SITES, expect
        json.dump(rows, open(os.path.join(OUT, "hydrogen_frames.json"), "w"))
        return {"frames": 3, "verdicts": json.dumps({"same": 1, "mixed": 1, "worse": 1}), "rows": P.rows_field(rows),
                "rows_file": "hydrogen_frames.json"}
    real_review, P.hydrogen_frames = P.hydrogen_frames, fake_review
    try:
        try:
            H.frames_step(cfg, "hydrogen_frames")
            raise AssertionError("a 'worse' with no override must stop")
        except RuntimeError as e:
            assert "worse" in str(e) and "a lead decision" in str(e)
        cfg["frame_overrides"] = {"void_c": {"verdict": "same", "by": "lead", "record": "LEAD_Y.md",
                                             "pair_sha256": [good("void_c")[0], "0" * 64]}}
        try:
            H.frames_step(cfg, "hydrogen_frames")
            raise AssertionError("a 'worse' with a mismatched override must still stop")
        except RuntimeError as e:
            assert "worse" in str(e) and "override errors" in str(e), e
        cfg["frame_overrides"] = {"void_c": {"verdict": "same", "by": "lead", "record": "LEAD_Y.md", "pair_sha256": good("void_c")},
                                  "wall_b": ov["wall_b"]}
        out = H.frames_step(cfg, "hydrogen_frames")
        assert json.loads(out["verdicts"]) == {"same": 2, "better": 1}, out["verdicts"]
        assert json.loads(out["hydrogen_verdicts"])["worse"] == 1
        assert len(json.loads(out["lead_overrides"])) == 2 and json.loads(out["override_errors"]) == []
        assert out["served"].startswith("SERVED.json") and out["capture_guard"].startswith("G0 v3 passed")
        # a frame changed after frames_ready -> stop (macmini re-shoots), before any review
        open(os.path.join(d, "wall_b__1.jpg"), "wb").write(b"changed")
        try:
            H.frames_step(cfg, "hydrogen_frames")
            raise AssertionError("a changed served frame must stop the review")
        except RuntimeError as e:
            assert "frames changed after frames_ready" in str(e)
    finally:
        P.hydrogen_frames = real_review
    print("frames_step: SERVED check, G0 v3, expect, overrides before the worse check, evidence fields: ok")

    # the accept prompt lists the lead overrides first
    seen = {}
    RL.coordinator_json = lambda prompt, schema, cwd, spec=None: seen.update(p=prompt) or {"decision": "accept", "reasons": []}
    RL.load_request = lambda: {"request_id": "t", "task": {"id": "highway_accept"},
                               "dependencies": {"hydrogen_frames": {k: str(v) for k, v in out.items()}, "d_walls": {"pass": "true"}}}
    RL.round_config = lambda: cfg
    RL.respond = lambda req, outcome="settled", output=None, error="": seen.update(out=output)
    RL.main()
    assert "Lead overrides (the lead's own verdicts" in seen["p"] and "LEAD_X.md" in seen["p"], seen["p"][:400]
    assert "Override errors" not in seen["p"]
    print("highway_accept's prompt lists the lead overrides before the evidence: ok")


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, write_through=True)
    for t in (t_served, t_sites, t_rows, t_site_of, t_g0_v3, t_overrides):
        t()
    shutil.rmtree(TMP)
    print("all frames tests pass")
