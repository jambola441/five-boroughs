#!/usr/bin/env python3
"""Pre-commit scan for the public five-boroughs repo. Exit 1 (and list why) if a staged file:
- contains a local path, a key/token/secret pattern, or Street View / Google imagery references;
- has a denied page-code name (drape_core*, set*_core*, inner_*.html, *.enc, page_pass, *.env);
- matches the sha256 of any local page code file (pipeline/tools/page_code_hashes.json).
Usage: python3 pipeline/tools/scan_commit.py  (scans `git diff --cached`)"""
import fnmatch, hashlib, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DENY_NAMES = ["drape_core*", "set?_core*", "inner_*.html", "*.enc", "page_pass*", "*.env", "supabase*"]
PATTERNS = [r"/Users/[^/\s]+", r"(?i)(api[_-]?key|secret|token|passphrase|password)\s*[=:]\s*\S{8,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY", r"(?i)streetview|maps\.googleapis|lh\d\.googleusercontent"]
HASHES = set(json.load(open(os.path.join(HERE, "page_code_hashes.json")))["hashes"])

files = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=AM"], capture_output=True,
                       text=True, check=True).stdout.split()
bad = []
for f in files:
    if f.endswith("page_code_hashes.json") or f.endswith("scan_commit.py"):
        continue
    if any(fnmatch.fnmatch(os.path.basename(f), p) for p in DENY_NAMES):
        bad.append(f"{f}: denied file name")
    blob = subprocess.run(["git", "show", f":{f}"], capture_output=True, check=True).stdout
    if hashlib.sha256(blob).hexdigest() in HASHES:
        bad.append(f"{f}: identical to local page code")
    text = blob.decode("utf-8", "replace")
    for p in PATTERNS:
        if re.search(p, text):
            bad.append(f"{f}: matches {p}")
if bad:
    print("COMMIT BLOCKED:\n  " + "\n  ".join(bad))
    sys.exit(1)
print(f"scan ok: {len(files)} staged files")
