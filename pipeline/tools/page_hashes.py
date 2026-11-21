#!/usr/bin/env python3
"""Refresh page_code_hashes.json: the sha256 of every file under the page editor's code trees, hashes only.
usage: PE_ROOT=<page editor folder> python3 -I pipeline/tools/page_hashes.py
Keeps the hashes already listed and adds those of $PE_ROOT's top-level files and of every file under
$PE_ROOT/phaseb and $PE_ROOT/src.
No names or paths are written, only hex digests."""
import hashlib, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "page_code_hashes.json")
root = os.environ.get("PE_ROOT") or sys.exit("set PE_ROOT")
old = set(json.load(open(OUT))["hashes"]) if os.path.exists(OUT) else set()
new = set()
for f in os.listdir(root):   # the page editor's own top-level files (specs, notes) too
    p = os.path.join(root, f)
    if os.path.isfile(p) and not os.path.islink(p):
        new.add(hashlib.sha256(open(p, "rb").read()).hexdigest())
for tree in ("phaseb", "src", "diag", "reach", "guards", "staged"):
    for d, _, fs in os.walk(os.path.join(root, tree)):
        for f in fs:
            p = os.path.join(d, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
            new.add(h.hexdigest())
json.dump({"note": "sha256 of local page code that must never be committed (hashes only)",
           "hashes": sorted(old | new)}, open(OUT, "w"), indent=0)
print(f"{len(new)} files hashed; list now {len(old | new)} hashes (was {len(old)})")
