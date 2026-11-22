"""Frames checks the highway rounds run around the Hydrogen review (the lead, 2026-11-22; POST_R14X2_LIST items 4 and 6,
and the lead's frame_overrides).

served(folder): write-once frames, the adapter half. SERVED.json lists every served file with its sha256. Every listed
  file must exist with that sha and an mtime <= READY's mtime, and the folder may hold no other image at its top level.
  Any mismatch, or no SERVED.json, is a re-shoot (never a pass). Why: a stray hidden tab overwrote 3 served r14t air
  panels after G0 and READY (r14x2-1).
sites_match(folder, need): the reviewer refuses a folder whose sites (stem.split("__")[0]) aren't exactly the request's
  poses, so a wrong folder fails loudly instead of producing one-panel "sites".
apply_overrides(rows, folder, overrides): the lead's per-pose verdicts, round-file field
  "frame_overrides": {pose: {"verdict", "by": "lead", "record", "pair_sha256": [sha of <pose>__1, sha of <pose>__2]}}.
  An override applies only when the reviewed files' sha256s equal pair_sha256 exactly, in panel order; otherwise it is
  ignored and reported in errors. An applied override replaces the row's verdict, keeps Hydrogen's own verdict and
  text under "hydrogen", and is marked "lead override". Nothing else changes a verdict: a "worse" passes only this way.
"""
import hashlib
import json
import os
import re

IMG = (".png", ".jpg", ".jpeg", ".webp")
VERDICTS = ("better", "same", "worse", "mixed")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def images(folder):
    return sorted(x for x in os.listdir(folder) if x.lower().endswith(IMG) and os.path.isfile(os.path.join(folder, x)))


def _served_files(s):
    """SERVED.json's {file: sha256}; files may sit under "files" (macmini's shape) or anywhere as {name: {sha256}}."""
    acc = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, dict) and "sha256" in v:
                    acc[os.path.basename(k)] = v["sha256"]
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(s)
    return acc


def served(folder):
    """(ok, why). ok False means re-shoot."""
    sp, rp = os.path.join(folder, "SERVED.json"), os.path.join(folder, "READY")
    if not os.path.exists(sp):
        return False, "no SERVED.json in the frames folder (write-once frames need it): re-shoot / re-serve"
    if not os.path.exists(rp):
        return False, "no READY in the frames folder"
    try:
        files = _served_files(json.load(open(sp)))
    except ValueError as e:
        return False, f"SERVED.json unreadable ({e}): re-serve"
    if not files:
        return False, "SERVED.json lists no files: re-serve"
    ready = os.path.getmtime(rp)
    have = images(folder)
    missing = [f for f in files if not os.path.exists(os.path.join(folder, f))]
    bad = [f for f in files if f not in missing and sha256(os.path.join(folder, f)) != str(files[f]).lower()]
    late = [f for f in files if f not in missing and os.path.getmtime(os.path.join(folder, f)) > ready]
    extra = [f for f in have if f not in files]
    probs = ([f"missing {missing[:6]}"] if missing else []) + ([f"sha mismatch {bad[:6]}"] if bad else []) + \
            ([f"written after READY {late[:6]}"] if late else []) + ([f"not in SERVED.json {extra[:6]}"] if extra else [])
    if probs:
        return False, "served frames don't match SERVED.json (" + "; ".join(probs) + "): re-shoot, never pass"
    return True, f"SERVED.json: {len(files)} files, shas match, none after READY"


def sites_of(folder):
    out = {}
    for n in images(folder):
        out.setdefault(os.path.splitext(n)[0].split("__")[0], []).append(n)
    return out


def sites_match(folder, need):
    """(ok, why): the folder's sites must be exactly the request's poses."""
    have = set(sites_of(folder))
    want = set(need)
    if have == want:
        return True, f"{len(have)} sites match the request"
    extra, missing = sorted(have - want), sorted(want - have)
    return False, (f"frames folder sites don't match the request's poses: "
                   + (f"not requested {extra[:8]}" if extra else "") + ("; " if extra and missing else "")
                   + (f"missing {missing[:8]}" if missing else ""))


def apply_overrides(rows, folder, overrides):
    """Returns (rows, applied, errors). rows are copied; Hydrogen's file on disk is never changed."""
    applied, errors, out = [], [], []
    overrides = overrides or {}
    by_site = {r.get("site"): r for r in rows}
    for pose, o in overrides.items():
        if pose not in by_site:
            errors.append(f"{pose}: override for a pose with no reviewed row (ignored)")
    for r in rows:
        o = overrides.get(r.get("site"))
        if o is None:
            out.append(r)
            continue
        pose, why = r["site"], []
        if not isinstance(o, dict):
            why.append("not an object")
            o = {}
        if o.get("by") != "lead":
            why.append(f"by must be 'lead' (got {o.get('by')!r})")
        if o.get("verdict") not in VERDICTS:
            why.append(f"verdict must be one of {VERDICTS} (got {o.get('verdict')!r})")
        if not o.get("record"):
            why.append("no record")
        want = o.get("pair_sha256")
        files = r.get("files") or []
        if not (isinstance(want, list) and want and all(isinstance(s, str) and HEX64.match(s) for s in want)):
            why.append("pair_sha256 must list lowercase 64-hex sha256s, one per panel file")
        elif len(want) != len(files):
            why.append(f"pair_sha256 has {len(want)} shas, the reviewed pair has {len(files)} files")
        else:
            got = [sha256(os.path.join(folder, f)) if os.path.exists(os.path.join(folder, f)) else None for f in files]
            if got != want:
                why.append("pair_sha256 doesn't match the served files exactly ("
                           + ", ".join(f"{f}: {g[:12] if g else 'missing'} != {w[:12]}"
                                       for f, g, w in zip(files, got, want) if g != w) + ")")
        if why:
            errors.append(f"{pose}: override ignored: " + "; ".join(why))
            out.append(r)
            continue
        new = {**r, "verdict": o["verdict"], "fixed": [], "new": [],
               "hydrogen": {"verdict": r.get("verdict"), "fixed": r.get("fixed", []), "new": r.get("new", [])},
               "override": f"lead override ({o['record']})"}
        out.append(new)
        applied.append({"pose": pose, "kind": "lead override", "hydrogen_verdict": r.get("verdict"),
                        "verdict": o["verdict"], "by": "lead", "record": o["record"], "pair_sha256": "matched"})
    return out, applied, errors
