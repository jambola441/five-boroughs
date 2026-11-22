"""G0 v2, the lead's capture guard for frame pairs (staged 2026-11-21; not used by r14w-1, whose frames run G0 v1).

Why (the lead): on the r14v frames 8 of 16 pairs failed G0 v1, all drive views, every air view passed. A removed
wall's shadows fall outside the unchanged-parts mask, so a real change can fail v1 for good, and v1 only loops back
to macmini.

v2, per pair, from <frames folder>/guards_frames.json rows:
  {"pose", "noise_a_pct" (A vs A' re-render, whole frame), "noise_b_pct" (B vs B', whole frame),
   "g0_pixel_diff_pct" (A vs B on the unchanged-parts mask)}
1. Capture noise: noise_a_pct and noise_b_pct must each be < lim, else that pair is re-shot (back to the renderer).
2. A vs B on the mask is then reported for every pair.
3. A pair whose A vs B is >= lim on clean captures is re-shot once; if it fails again on a clean re-shoot, it is a
   lead decision (no further loop). History per pose is kept in history_path across READY drops.

Switch it on in a round file: frames.capture_guard.version = 2 (wired in highway.frames_step; r14x is the first user).
"""
import hashlib
import json
import os


def site_of(pose, sites):
    """The one site a G0 row belongs to: its exact name, else the LONGEST site name the pose contains (so
    'void_x-3_z15_drive' is never counted for 'void_x-3_z15')."""
    pose = str(pose or "")
    if pose in sites:
        return pose
    hits = [s for s in sites if s in pose]
    return max(hits, key=len) if hits else None


def evaluate(folder, need=(), lim=0.05, history_path=None):
    """Returns (state, report, rows). state: 'pass' | 'reshoot' | 'lead'."""
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
    rows, noisy, fail_once, fail_again = [], [], [], []
    for x in pairs:
        pose, na, nb, ab = x.get("pose"), x.get("noise_a_pct"), x.get("noise_b_pct"), x.get("g0_pixel_diff_pct")
        clean = na is not None and nb is not None and na < lim and nb < lim
        h = hist.setdefault(str(pose), {})
        h[shot] = {"clean": clean, "ab": ab}
        bad_clean = [s for s, v in h.items() if v["clean"] and v["ab"] is not None and v["ab"] >= lim]
        rows.append({"pose": pose, "noise_a_pct": na, "noise_b_pct": nb, "ab_masked_pct": ab, "clean": clean,
                     "ab_fails_on_clean_shoots": len(bad_clean)})
        if not clean:
            noisy.append(pose)
        elif ab is None or ab >= lim:
            (fail_again if len(bad_clean) >= 2 else fail_once).append(pose)
    if history_path:
        json.dump(hist, open(history_path, "w"), indent=1)
    rep = "; ".join(f"{r['pose']}: noise A {r['noise_a_pct']} B {r['noise_b_pct']}, A vs B {r['ab_masked_pct']} %" for r in rows)
    if noisy or fail_once:
        why = (f"capture noise >= {lim} % at {', '.join(map(str, noisy))}" if noisy else "") + \
              ("; " if noisy and fail_once else "") + \
              (f"A vs B >= {lim} % on clean captures at {', '.join(map(str, fail_once))} (first time: re-shoot)" if fail_once else "")
        return "reshoot", why, rows
    if fail_again:
        return "lead", (f"A vs B >= {lim} % on two clean shoots at {', '.join(map(str, fail_again))}: a real change outside "
                        f"the mask (e.g. a removed wall's shadow) or a lasting capture difference; the lead decides"), rows
    return "pass", f"G0 v2 passed: {len(rows)} pairs, capture noise and A vs B under {lim} % ({rep[:600]})", rows
