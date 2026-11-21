#!/usr/bin/env python3
"""Live terrain round: terrain study build/checks/freeze + drive-test QA gates, every gate a mandatory step.

Wraps each session's EXISTING scripts unchanged (specs: terrain_study/p2/ROUND_GRAPH_SPEC_TERRAIN.md,
drive_audit/ROUND_GRAPH_SPEC.md). Exact counts come only from those scripts; pass/fail is computed here in code;
model steps only judge. mode "existing" = dry run on an already frozen round: nothing is built, frozen or re-run,
the steps read the outputs that already exist.

No step uploads, pushes, downloads or deletes. Cleanup stays with the drive test under the owner's page_drape rule.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # -I drops the script dir

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess

from common import coordinator_json, deps, load_request, project, respond, round_config, run_job, run_main, validate, OUT

TS = "work/terrain_study"
DA = "work/drive_audit"
PY = os.path.join(os.environ.get("NYC_ROOT", ""), ".venv/bin/python")


def sh(cmd, cwd, log, env=None, timeout=None):
    """Run one of the team's own commands unchanged, as a detached job (see common.run_job); output goes to log."""
    return run_job(os.path.splitext(log)[0], cmd, cwd, log, env=env)


def last(path, n=4000):
    with open(path) as f:
        return f.read()[-n:]


# ---------- terrain ----------

def t_build(cfg):
    folder = project(f"{TS}/p1/terrain/{cfg['round']}")
    if cfg["mode"] == "existing":
        frozen = os.path.isdir(folder) and not (os.stat(folder).st_mode & stat.S_IWUSR)
        return {"built": "existing", "frozen": str(frozen).lower()}, None
    started = os.path.exists(os.path.join(OUT, "jobs", "terrain_build", "job.json"))   # our own job made it
    if os.path.exists(folder) and not started:
        return None, f"{folder} already exists; rounds are write-once, pick a new name"
    rc = sh(["zsh", cfg["build_script"]], project(TS), "terrain_build.log")
    if rc != 0:
        return None, f"round script exited {rc}; see terrain_build.log"
    return {"built": "new", "frozen": "false"}, None


def t_checks(cfg):
    r = cfg["round"]
    # Lead (21 Nov): free ends include live's lines in the 605 fallback tiles -> the terrain study's separate
    # _free_ends_fb.json (same format). The old _free_ends.json is never used for the gate.
    fb = project(f"{TS}/p2/checks/{r}_free_ends_fb.json")
    if not os.path.exists(fb):
        raise FileNotFoundError(f"{r}_free_ends_fb.json not written yet")
    fe = json.load(open(fb))["summary"]
    free_ends = sum(v.get("ends", 0) for v in fe.values() if isinstance(v, dict))
    cr = json.load(open(project(f"{TS}/p2/checks/{r}_crest_census.json")))["summary"]["by_kind"]
    crest8 = cr["raised"]["ge_8pct_near_end"] + cr["ext"]["ge_8pct_near_end"]
    crest12 = cr["raised"]["ge_12pct_near_end"] + cr["ext"]["ge_12pct_near_end"]
    # widened census (every deck line incl. the fallback tiles, any kind), when the terrain study wrote one
    wp = project(f"{TS}/p2/checks/{r}_crest_census_widened.json")
    wd = json.load(open(wp))["summary"]["widened"] if os.path.exists(wp) else {}
    js = json.load(open(project(f"{TS}/p2/checks/{r}_join_steps.json")))["summary"]["steps_gt_0p1"]
    # the byte compare vs the previous round: the terrain study's round log, or (graph builds) this run's build log
    logs = [project(f"{TS}/p2/work/v1c_r6/{r.replace('v1c_', '')}_round.log"), os.path.join(OUT, "terrain_build.log")]
    log = next((open(p).read() for p in logs if os.path.exists(p)), "")
    diff, deck_names = {}, []
    for k, v in re.findall(r"^(tiles|water|skirts|decks) \S+ vs \S+ differing: (.+)$", log, re.M):
        if v.strip().startswith("["):   # newer scripts list the changed deck files by name
            deck_names = re.findall(r"'([^']+)'", v)
            diff[k] = len(deck_names)
        else:
            diff[k] = int(v)
    if deck_names:
        diff["deck_names"] = deck_names
    # junction pairs over 2 m: none may be NEW vs the previous round's (x, z within 2 m and step within 0.3 m)
    pp = [project(f"{TS}/p2/checks/{x}_deck_checks_pairs.json") for x in (r, cfg.get("prev", ""))]
    if all(os.path.exists(x) for x in pp):
        cur, prev = (json.load(open(x))["_junction_pairs_over_2m"]["after"] for x in pp)
        new = [q for q in cur if not any(math.hypot(q["x"] - o["x"], q["z"] - o["z"]) <= 2
                                         and abs(q["step"] - o["step"]) <= 0.3 for o in prev)]
        diff["new_junction_pairs"] = [f"{q['step']} m at {q['x']:.0f},{q['z']:.0f}" for q in new]
    # junctions: the deck checks' own JSON when written (graph rounds), else the round log
    dc = project(f"{TS}/p2/checks/{r}_deck_checks.json")
    if os.path.exists(dc):
        ja = json.load(open(dc))["_junctions"]["after"]
        jm = (ja["over_2"], ja["max"])
    else:
        m = re.search(r"'after': \{[^}]*'over_2': (\d+), 'max': ([\d.]+)", log)
        jm = (int(m.group(1)), float(m.group(2))) if m else (-1, -1)
    return {"free_ends": free_ends, "crest_8pct": crest8, "crest_12pct": crest12,
            "crest_widened_8": wd.get("ge8_near_end", -1), "crest_widened_12": wd.get("ge12_near_end", -1),
            "join_steps_gt_0p1": js,
            "junctions_over_2": jm[0], "junctions_max": jm[1],
            "differ": json.dumps(diff)}


def t_rules(c, cfg):
    p = cfg["prev_terrain"]
    diff = json.loads(c.get("differ", "{}"))
    fails = []
    if int(c["free_ends"]) > p["free_ends"]: fails.append(f"free ends {c['free_ends']} > {p['free_ends']}")
    if int(c["junctions_over_2"]) > p["junctions_over_2"]: fails.append("junctions over 2 m rose")
    if float(c["junctions_max"]) > p["junctions_max"] + 0.5: fails.append("junction max rose > 0.5 m")
    if int(c["crest_8pct"]) > p["crest_8pct"] or int(c["crest_12pct"]) > p["crest_12pct"]:
        if not cfg.get("lead_ok_crest"): fails.append("crest census rose without the lead's OK")
    if "crest_widened_ge8_near_end" in p:   # widened baseline: the round must have measured it, and not risen
        w8, w12 = int(c.get("crest_widened_8", -1)), int(c.get("crest_widened_12", -1))
        if w8 < 0:
            fails.append("widened crest census not written for this round")
        elif (w8 > p["crest_widened_ge8_near_end"] or w12 > p["crest_widened_ge12_near_end"]) and not cfg.get("lead_ok_crest"):
            fails.append(f"widened crest census {w8}/{w12} rose vs {p['crest_widened_ge8_near_end']}/"
                         f"{p['crest_widened_ge12_near_end']} without the lead's OK")
    if int(c["join_steps_gt_0p1"]) > p["join_steps_gt_0p1"]: fails.append("join steps rose")
    for k in ("tiles", "water", "skirts"):
        if diff.get(k, 0) > cfg.get("planned_diff", {}).get(k, 0): fails.append(f"{k} changed outside the plan")
    plan_tiles = cfg.get("planned_diff", {}).get("deck_tiles")
    # with named tiles, the count may differ (a new deck file where a tile fell back to live); the names may not
    if not plan_tiles and "decks" in diff and diff["decks"] != cfg.get("planned_diff", {}).get("decks", diff["decks"]):
        fails.append(f"deck files changed {diff['decks']} != plan {cfg['planned_diff']['decks']}")
    if diff.get("new_junction_pairs"):
        fails.append(f"new junction pairs over 2 m vs {cfg.get('prev')}: {diff['new_junction_pairs'][:5]}")
    if "new_junction_pairs" not in diff and cfg.get("prev_terrain", {}).get("junction_pairs_rule", True):
        fails.append("junction pairs file missing for this round or the previous one")
    if plan_tiles and diff.get("deck_names"):   # each changed deck file must belong to a planned tile
        off = [f for f in diff["deck_names"] if os.path.splitext(f)[0] not in {f"tile_{t}" for t in plan_tiles}]
        if off: fails.append(f"deck files changed outside the planned tiles: {off[:6]}")
    if int(c["junctions_over_2"]) < 0: fails.append("junction check not found")
    if not diff: fails.append("no byte compare vs the previous round found")
    return fails


def t_freeze(cfg):
    r = cfg["round"]
    pairs = ((f"{TS}/p1/terrain/{r}", f"{TS}/p2/out/manifest_terrain_{r}_sha256.json"),
             (f"{TS}/p1/bases_h16_t1c_{r.replace('v1c_', '')}", f"{TS}/p2/out/manifest_bases_h16_t1c_{r.replace('v1c_', '')}_sha256.json"))
    h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
    shas = {}
    for loc, man in pairs:
        loc, man = project(loc), project(man)
        M = json.load(open(man))
        files = {os.path.relpath(os.path.join(d, f), loc) for d, _, fs in os.walk(loc) for f in fs}
        bad = [k for k, v in M.items() if h(os.path.join(loc, k)) != v]
        if bad or files != set(M):
            return None, f"{loc}: {len(bad)} mismatched, {len(files ^ set(M))} extra/missing files"
        shas[os.path.basename(man)] = h(man)
    if cfg["mode"] != "existing":
        for loc, _ in pairs:
            subprocess.run(["chmod", "-R", "a-w", project(loc)], check=True)
    tsha = shas[f"manifest_terrain_{r}_sha256.json"]
    return {"terrain_sha": tsha, "verified": "true",
            "matches_expected": str(cfg.get("expected_sha") in (None, "", tsha)).lower()}, None


# ---------- QA ----------

def q_round_gate(cfg, sha):
    tv = cfg["round"]
    o = project(f"{DA}/deckends/lift_pd_{tv}_r14s_lr3")
    if cfg["mode"] == "existing":
        log = project(f"{DA}/deckends/run_round_gate_{tv.replace('v1c_', '')}.log")
        if not os.path.exists(log):
            return None, "no existing gate log for this round", "not_started"
        return {"gate_log": log, "gate_dir": o, "ran": "existing"}, None, None
    env = {k: v for k, v in (cfg.get("qa_env") or {}).items()}
    pts = project(f"{TS}/p2/checks/{tv}_crest_points.json")   # terrain study writes it per round
    if "CREST_POINTS" not in env and os.path.exists(pts):
        env["CREST_POINTS"] = pts
    rc = sh(["./run_round_gate.sh", tv, sha], project(DA), "qa_round_gate.log", env=env, timeout=4 * 3600)
    if rc == 2:
        return None, "manifest sha differs from the pin", "settled_fail"
    if rc == 3:
        return None, "disk guard: not enough free space; free space, then retry", "not_started"
    if rc != 0:
        return None, f"run_round_gate.sh exited {rc}", "uncertain"
    return {"gate_log": os.path.join(OUT, "qa_round_gate.log"), "gate_dir": o, "ran": "live"}, None, None


def gate_numbers(g):
    """Every number QA judges, from GATE.json when the drive test writes it, else from its log lines."""
    gj = os.path.join(g["gate_dir"], "GATE.json")
    if os.path.exists(gj):
        return json.load(open(gj))
    t = open(g["gate_log"]).read()
    n = {}
    m = re.findall(r"^(START|END) \S+ manifest \S+ (MATCH|DIFFERS)", t, re.M)
    mm = re.findall(r"^(START|END) files \d+ mismatch (\d+)", t, re.M)
    r14 = re.findall(r"r14s sha list OK: (\d+) / 298", t)
    n["pins"] = {"start_match": ("START", "MATCH") in m, "end_match": ("END", "MATCH") in m,
                 "r14s_ok": min(map(int, r14)) if r14 else 0,
                 "mismatch_start": int(dict(mm).get("START", -1)), "mismatch_end": int(dict(mm).get("END", -1))}
    a = re.search(r"\(a\) SAMEPOINT visible (\d+), same as live (\d+), new (\d+)", t)
    if a: n["a"] = {"visible": int(a[1]), "same": int(a[2]), "new": int(a[3])}
    c = re.search(r"\(c\) \(ii\) vs live: ([\d,]+) m \(\S+ ([\d,]+)\); new vs \S+: ([\d,]+) m, (\d+) places", t)
    ii = re.search(r"split \(ii\) ([\d,]+) m at (\d+) places", t)
    vp = re.search(r"_vs_prev: .*?: (\d+) flags at (\d+) places", t)
    if c: n["c"] = {"ii_m": int(c[1].replace(",", "")), "prev_ii_m": int(c[2].replace(",", "")),
                    "new_ge1m_places": int(c[4]), "ii_places": int(ii[2]) if ii else -1,
                    "vs_prev_new_m": int(vp[1]) if vp else -1, "vs_prev_new_places": int(vp[2]) if vp else -1}
    r = re.search(r"RAMPGATE \S+ flattened (\d+) \(previous (\d+)\); new (\d+), gone (\d+)", t)
    if r: n["ramps"] = {"flattened": int(r[1]), "prev": int(r[2]), "new": int(r[3]), "gone": int(r[4])}
    cl = re.search(r"CITY car ledges (\d+) >= 2 m (\d+) \| car steps >= 2 m v2 (\d+) v1 (\d+) \| car raised (\d+)", t)
    cu = re.search(r"CURBS .*?= (\d+) m of car route at (\d+) places", t)
    if cl: n["city"] = {"ledges": int(cl[1]), "ledges_2m": int(cl[2]), "steps_2m_v2": int(cl[3]),
                        "steps_2m_v1": int(cl[4]), "raised": int(cl[5]),
                        "curbs_m": int(cu[1]) if cu else -1, "curbs_places": int(cu[2]) if cu else -1,
                        "error_chunks": 0 if "city error chunks" not in t else
                        int(re.search(r"city error chunks: (\d+)", t)[1])}
    du = os.path.join(g["gate_dir"], "deck_under.log")
    if os.path.exists(du):
        n["b"] = json.loads(open(du).readline())
    return n


def q_rule(name, n, cfg):
    """Pass/fail in code. Returns (passed, reason)."""
    R = cfg.get("rules", {})
    if name == "pins":
        p = n.get("pins") or {}
        ok = p.get("start_match") and p.get("end_match") and p.get("r14s_ok") == 298 \
            and p.get("mismatch_start") == 0 and p.get("mismatch_end") == 0
        return ok, json.dumps(p)
    if name not in n:
        return None, f"no {name} measurement yet"
    x = n[name]
    if name == "a":
        return x["new"] == 0, f"visible {x['visible']}, same as live {x['same']}, new {x['new']}"
    if name == "c":
        ok = x["ii_m"] <= x["prev_ii_m"] and x["new_ge1m_places"] == 0
        return ok, f"(ii) {x['ii_m']} m vs previous {x['prev_ii_m']}; new >= 1 m places {x['new_ge1m_places']}"
    if name == "ramps":
        ok = x["flattened"] <= x["prev"] and x["new"] == 0
        return ok, f"flattened {x['flattened']} (previous {x['prev']}), new {x['new']}"
    if name == "city":
        return x.get("error_chunks", 1) == 0, f"error chunks {x.get('error_chunks')}; counts reported"
    if name == "b":
        # Lead rule (21 Nov): underM <= previous +1%, linesUnder <= previous, noneM ("nothing drawn") <= previous +1%.
        rule = R.get("b")
        if not rule: return None, "the lead has not set a pass rule for gate (b) yet"
        P, tol = cfg["prev_qa"], rule.get("tolerance", 0.01)
        if cfg.get("prev_qa_authoritative"):   # the lead set the baseline in the round file: it wins over GATE.json's
            pu, pn, pl = P["b_underM"], P["b_noneM"], P["b_linesUnder"]
        else:
            pu = x.get("prev_underM", P["b_underM"])
            pn = x.get("prev_noneM", P["b_noneM"])
            pl = x.get("prev_linesUnder", P["b_linesUnder"])
        ok = x["underM"] <= pu * (1 + tol) and x["linesUnder"] <= pl and x["noneM"] <= pn * (1 + tol)
        return ok, f"underM {x['underM']} vs {pu}; noneM {x['noneM']} vs {pn}; linesUnder {x['linesUnder']} vs {pl} (+{tol:.0%})"
    if name == "walls":
        # Lead rule: terrain rounds = no rise vs the previous round on the same highway build;
        # highway rounds = on-road metres <= live's.
        rule = R.get("walls")
        if not rule: return None, "the lead has not set a pass rule for walls on terrain rounds yet"
        lim = cfg["prev_qa"]["walls_onroad_m"] if rule.get("kind") == "no_rise" else x["live_m"]
        return x["onroad_m"] <= lim + 1e-6, f"on-road {x['onroad_m']} m vs limit {lim} ({rule.get('kind')})"
    return None, "unknown gate"


def bundle_verdict(n, cfg):
    """Shippable vs LIVE (lead, 21 Nov): separate from the round's pass/fail. Every absolute gate against live must pass."""
    lines, blocks = [], []
    c, w, a, r = n.get("c"), n.get("walls"), n.get("a"), n.get("ramps")
    if c is None: blocks.append("lower than live (ii): not measured")
    elif c["ii_m"] > 0: blocks.append(f"lower than live (ii): {c['ii_m']} m at {c.get('ii_places')} places (must be 0)")
    if w is None: blocks.append("walls on road: not measured")
    elif w["onroad_m"] > w["live_m"]: blocks.append(f"walls on road: {w['onroad_m']} m > live {w['live_m']} m")
    if a is None: blocks.append("free ends: not measured")
    elif a["new"] > 0 or a["same"] != a["visible"]: blocks.append(f"free ends: {a['visible']} visible, {a['same']} same as live")
    live_r = cfg.get("live_ramps_flattened") if r is None else r.get("live", cfg.get("live_ramps_flattened"))
    if r is None: blocks.append("ramp census: not measured")
    elif live_r is None: blocks.append("ramp census: no live baseline yet")
    elif r["flattened"] > live_r: blocks.append(f"ramp census: {r['flattened']} flattened > live {live_r}")
    return {"shippable": str(not blocks).lower(), "blocking": json.dumps(blocks),
            "line": "BUNDLE: shippable" if not blocks else "BUNDLE: NOT shippable: " + "; ".join(blocks)}


def main():
    req = load_request()
    tid = req["task"]["id"]
    if "valid" in (req.get("kind") or ""):
        c = req.get("candidate") or {}
        if tid == "t_build": return validate(req, c.get("built") in ("new", "existing"))
        if tid == "t_checks": return validate(req, c.get("pass") == "true")
        if tid == "t_freeze": return validate(req, c.get("verified") == "true" and c.get("matches_expected") == "true")
        if tid.startswith("gate_"): return validate(req, c.get("pass") == "true")
        if tid in ("terrain_accept", "qa_accept", "highway_accept"):
            return validate(req, c.get("rules_pass") == "true" and c.get("decision") == "accept")
        return validate(req, True)

    cfg = round_config()
    d = deps(req)
    if tid == "t_build":
        out, err = t_build(cfg)
        return respond(req, output=out) if out else respond(req, "settled", error=err)
    if tid == "t_checks":
        try:
            c = t_checks(cfg)
        except FileNotFoundError as e:
            return respond(req, "not_started", error=str(e))
        fails = t_rules(c, cfg)
        c.update({"pass": str(not fails).lower(), "fails": json.dumps(fails)})
        return respond(req, output=c)
    if tid == "t_freeze":
        out, err = t_freeze(cfg)
        return respond(req, output=out) if out else respond(req, "settled", error=err)
    if tid == "qa_disk":
        free = shutil.disk_usage(project(".")).free / 1e9
        if free < 9 and cfg["mode"] != "existing":   # a dry run re-runs nothing, so it needs no space  # the drape needs ~4 GB and the drive test's guard keeps 5 GB free
            return respond(req, "not_started", error=f"only {free:.1f} GB free; need 9 GB")
        return respond(req, output={"free_gb": round(free, 1)})
    if tid == "qa_round_gate":
        sha = d.get("t_freeze", {}).get("terrain_sha", "")
        out, err, kind = q_round_gate(cfg, sha)
        if out:
            return respond(req, output=out)
        return respond(req, {"not_started": "not_started", "uncertain": "uncertain"}.get(kind, "settled"), error=err)
    if tid == "frames_ready":
        pairs = cfg.get("frame_pairs") or {}
        if cfg.get("frames_outside_graph") and not pairs:   # the lead's choice: frames rendered and reviewed apart
            return respond(req, output={"pairs": "0 (frames outside this graph)"})
        missing = [k for k, (b, a) in pairs.items() if not all(os.path.exists(project(p)) for p in (b, a) if p)]
        if not pairs or missing:
            return respond(req, "not_started", error=f"awaiting frames ({len(pairs)} pairs listed, missing {missing})")
        return respond(req, output={"pairs": len(pairs)})
    if tid == "gate_flight":
        rule = cfg.get("rules", {}).get("flight")
        fl = gate_numbers(d["qa_round_gate"]).get("flight") if "qa_round_gate" in d else None
        if rule and fl:  # drive test's GATE.json: {"pieces": N, "max_flight_cm_at_80": x, "points_without_route": k}
            if fl.get("points_without_route", 0) > 0:
                return respond(req, "settled", error=f"{fl['points_without_route']} crest points found no car route; fix the points file")
            ok = fl["max_flight_cm_at_80"] <= rule["max_cm_at_80"]
            return respond(req, output={"pass": str(ok).lower(),
                                        "detail": f"{fl['pieces']} new pieces, worst flight {fl['max_flight_cm_at_80']} cm at 80 km/h (max {rule['max_cm_at_80']})"})
        res = cfg.get("flight_result")
        if not rule or not res or not os.path.exists(project(res)):
            return respond(req, "not_started", error="awaiting the lead's flight rule and the crest/flight result for the new pieces")
        rows = json.load(open(project(res)))
        bad = [r for r in (rows if isinstance(rows, list) else rows.get("rows", []))
               if r.get("flight_cm_at_80", 0) > rule["max_cm_at_80"]]
        return respond(req, output={"pass": str(not bad).lower(), "detail": f"{len(bad)} pieces over {rule['max_cm_at_80']} cm at 80 km/h"})
    if tid == "bundle":
        return respond(req, output=bundle_verdict(gate_numbers(d["qa_round_gate"]), cfg))
    if tid.startswith("gate_"):
        n = gate_numbers(d["qa_round_gate"])
        ok, why = q_rule(tid[len("gate_"):], n, cfg)
        if ok is None:
            return respond(req, "not_started", error=why)  # no rule or no number yet: blocks, never passes
        return respond(req, output={"pass": str(bool(ok)).lower(), "detail": why})
    if tid in ("terrain_accept", "qa_accept", "highway_accept"):
        gates = {k: v for k, v in d.items() if k.startswith(("gate_", "d_")) or k in ("t_checks", "t_build", "t_freeze")}
        # the BUNDLE line never decides the round: it is reported beside it
        rules_pass = all(v.get("pass", "true") == "true" for v in gates.values())
        role = tid.replace("_accept", "")
        prompt = (open(os.path.join(os.path.dirname(__file__), "..", "prompts", f"{role}_accept_live.md")).read()
                  + f"\n\nRound: {cfg['round']} (previous {cfg['prev']})"
                  + f"\n\nPlanned changes vs the previous round: {json.dumps(cfg.get('planned_diff', {}))}"
                  + f"\n\nGate verdicts, computed in code: {'ALL PASS' if rules_pass else 'AT LEAST ONE FAIL'}"
                  + "\n\nEvidence:\n" + json.dumps(d, indent=1)[:16000])
        schema = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["accept", "reject"]},
                                                     "reasons": {"type": "array", "items": {"type": "string"}}},
                  "required": ["decision", "reasons"]}
        try:
            v = coordinator_json(prompt, schema, cwd=OUT, spec=cfg.get("coordinator"))
        except Exception as e:
            return respond(req, "uncertain", error=f"coordinator failed: {e}"[:300])
        out = {"rules_pass": str(rules_pass).lower(), "decision": v["decision"], "reasons": json.dumps(v["reasons"])}
        if tid == "qa_accept":
            out["bundle"] = d.get("bundle", {}).get("line", "not measured")
        return respond(req, output=out)
    respond(req, "not_started", error=f"unknown task {tid}")


if __name__ == "__main__":
    run_main(main)
