"""G0 v3, the lead's capture guard for frame pairs (POST_R14X2_LIST item 5; the lead, 2026-11-22). v2 stays as it was
(g0_v2.py) so v2 rounds keep their code; a round switches with frames.capture_guard.version = 3.

Everything in v2 holds (sun_set, capture noise, A vs B on the unchanged-parts mask, the repeat rule, the 3-invalid cap),
plus:
(c) mask coverage: every row must carry mask_coverage_pct (% of the frame the changed-parts mask covers). A row
    without it is an invalid shoot (re-shoot). Coverage > max_mask_coverage_pct (default 25) sends the pair to the
    lead: a mask that big proves little on the unchanged parts (a sanity cap; wallsite_10 sits at 19.6 %).
(b) a row's lead_rule is honoured only if the round file lists it in frames.capture_guard.lead_rules (the lead writes
    round files; a renderer can't grant itself a rule) and the row names its lead_record. Any other lead_rule -> the
    lead. Rules:
    (a) twin_match (LEAD_G0_DECISION.md): a pose with several capture sets passes when one previous-round capture
        and one this-round capture match < lim on the mask and each has a same-round twin < lim. The row carries the
        picks (picked_tags A, A2, B, B2) and their numbers: noise_a_pct = A vs A2, noise_b_pct = B vs B2,
        g0_pixel_diff_pct = A vs B (masked). Twins over lim -> re-shoot; twins clean but A vs B over lim -> the lead at
        once (a cross-round match is the proof; no re-shoot loop).
    hw_streaming (LEAD_HYDROGEN_WORSE_DECISION.md): non-highway layer counts equal
        (layers_A_vs_B_equal_excl_highways true), whole frame < lim (whole_frame_pct, else g0_pixel_diff_pct, which
        is whole-frame in such rows), twins < lim; highway counts logged (hw_tris or hw_counts present), not gated.
        Failing numbers -> re-shoot; equal-layers false or no highway counts -> an invalid shoot (re-shoot).
Returns (state, report, rows) like v2. state: 'pass' | 'reshoot' | 'lead'.
"""
import hashlib
import json
import os

from g0_v2 import CAPTURE_CAP, site_of

RULES = ("twin_match", "hw_streaming")


def _lt(v, lim):
    return isinstance(v, (int, float)) and v < lim


def evaluate(folder, need=(), lim=0.05, history_path=None, require_sun=True, cap=CAPTURE_CAP,
             max_cov=25.0, lead_rules=()):
    f = os.path.join(folder, "guards_frames.json")
    if not os.path.exists(f):
        return "reshoot", "no guards_frames.json in the frames folder", []
    raw = open(f, "rb").read()
    pairs = json.loads(raw).get("pairs", [])
    have = {site_of(x.get("pose"), list(need)) for x in pairs} if need else set()
    missing = [n for n in need if n not in have]
    if missing:
        return "reshoot", f"no G0 row for {len(missing)} of {len(need)} pairs: {', '.join(missing[:6])}", []
    shot = hashlib.sha256(raw).hexdigest()[:12]   # one READY drop = one shoot
    hist = json.load(open(history_path)) if history_path and os.path.exists(history_path) else {}
    invalid = lambda pose: sum(1 for v in hist.get(str(pose), {}).values() if v.get("invalid"))

    def save():
        if history_path:
            json.dump(hist, open(history_path, "w"), indent=1)

    # invalid shoots first (no sun, no mask coverage): re-shoot; they never count toward the A vs B repeat rule
    bad_shoot = {}
    for x in pairs:
        p = str(x.get("pose"))
        if require_sun and x.get("sun_set") is not True:
            bad_shoot[p] = "no sun"
        elif not isinstance(x.get("mask_coverage_pct"), (int, float)):
            bad_shoot[p] = "no mask coverage"
    if bad_shoot:
        for p, why in bad_shoot.items():
            hist.setdefault(p, {})[shot] = {"invalid": why}
        save()
        over = [p for p in bad_shoot if invalid(p) >= cap]
        if over:
            return "lead", (f"{len(over)} pose(s) with {cap} invalid captures: {', '.join(over[:8])}; "
                            f"the capture won't settle, the lead decides"), []
        return "reshoot", ("invalid shoot, re-shoot: " + "; ".join(f"{p} ({w})" for p, w in list(bad_shoot.items())[:8])), []

    rows, noisy, fail_once, fail_again, lead = [], [], [], [], []
    for x in pairs:
        pose, na, nb, ab = x.get("pose"), x.get("noise_a_pct"), x.get("noise_b_pct"), x.get("g0_pixel_diff_pct")
        rule, cov = x.get("lead_rule"), x["mask_coverage_pct"]
        h = hist.setdefault(str(pose), {})
        row = {"pose": pose, "noise_a_pct": na, "noise_b_pct": nb, "ab_masked_pct": ab, "mask_coverage_pct": cov,
               **({"lead_rule": rule} if rule else {})}
        rows.append(row)
        if cov > max_cov:
            lead.append(f"{pose}: mask covers {cov} % of the frame (> {max_cov} %)")
            h[shot] = {"lead": "mask coverage"}
            continue
        if rule:
            if rule not in RULES or rule not in lead_rules or not x.get("lead_record"):
                lead.append(f"{pose}: lead_rule {rule!r} not granted by this round file (granted: {list(lead_rules)})"
                            + ("" if x.get("lead_record") else ", no lead_record"))
                h[shot] = {"lead": "rule not granted"}
                continue
            if rule == "twin_match":
                picks = x.get("picked_tags") or {}
                if not all(picks.get(k) for k in ("A", "A2", "B", "B2")):
                    h[shot] = {"invalid": "twin_match without picked_tags A, A2, B, B2"}
                    noisy.append(pose)
                elif not (_lt(na, lim) and _lt(nb, lim)):
                    h[shot] = {"invalid": "twins over the limit"}
                    noisy.append(pose)
                elif not _lt(ab, lim):
                    h[shot] = {"clean": True, "ab": ab}
                    lead.append(f"{pose}: twin_match, no cross-round pair under {lim} % (A vs B {ab} %) with clean twins")
                else:
                    h[shot] = {"clean": True, "ab": ab}
                row["clean"] = _lt(na, lim) and _lt(nb, lim)
                continue
            # hw_streaming
            whole = x.get("whole_frame_pct", ab)
            row["whole_frame_pct"] = whole
            if x.get("layers_A_vs_B_equal_excl_highways") is not True or not (x.get("hw_tris") or x.get("hw_counts")):
                h[shot] = {"invalid": "hw_streaming without equal non-highway layers or highway counts"}
                noisy.append(pose)
            elif not (_lt(na, lim) and _lt(nb, lim) and _lt(whole, lim)):
                h[shot] = {"invalid": "hw_streaming numbers over the limit"}
                noisy.append(pose)
            else:
                h[shot] = {"clean": True, "ab": whole}
            row["clean"] = pose not in noisy
            continue
        # no lead rule: v2's rules
        clean = _lt(na, lim) and _lt(nb, lim)
        h[shot] = {"clean": clean, "ab": ab, **({} if clean else {"invalid": "noise"})}
        bad_clean = [s for s, v in h.items() if v.get("clean") and v.get("ab") is not None and v["ab"] >= lim]
        row.update(clean=clean, ab_fails_on_clean_shoots=len(bad_clean))
        if not clean:
            noisy.append(pose)
        elif ab is None or ab >= lim:
            (fail_again if len(bad_clean) >= 2 else fail_once).append(pose)
    save()
    rep = "; ".join(f"{r['pose']}: noise A {r['noise_a_pct']} B {r['noise_b_pct']}, A vs B {r['ab_masked_pct']} %, "
                    f"mask {r['mask_coverage_pct']} %" + (f" ({r['lead_rule']})" if r.get("lead_rule") else "") for r in rows)
    capped = [p for p in noisy if invalid(p) >= cap]
    if capped:
        lead.append(f"{len(capped)} pose(s) with {cap} invalid captures: {', '.join(map(str, capped[:8]))}")
    if lead:
        return "lead", "; ".join(lead)[:900] + "; the lead decides", rows
    if noisy or fail_once:
        why = (f"capture noise / twins >= {lim} % or invalid rule data at {', '.join(map(str, noisy))}" if noisy else "") + \
              ("; " if noisy and fail_once else "") + \
              (f"A vs B >= {lim} % on clean captures at {', '.join(map(str, fail_once))} (first time: re-shoot)" if fail_once else "")
        return "reshoot", why, rows
    if fail_again:
        return "lead", (f"A vs B >= {lim} % on two clean shoots at {', '.join(map(str, fail_again))}: a real change outside "
                        f"the mask or a lasting capture difference; the lead decides"), rows
    return "pass", f"G0 v3 passed: {len(rows)} pairs ({rep[:700]})", rows
