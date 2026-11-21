#!/usr/bin/env python3
"""Pre-commit scan for the public five-boroughs repo. Exit 1 (and list why) unless EVERY staged file passes.

Layer 1, ALLOWLIST: a commit may only add or change these paths, of these types:
  .gitignore, README.md, pipeline/README.md, pipeline/POLICY.md, pipeline/build_round.py,
  pipeline/adapters/*.py, pipeline/prompts/*.md, pipeline/rounds/*.json,
  pipeline/tools/*.py | *.sh | *.html | page_code_hashes.json
Anything else (data, page code, anything from the team's work folders, other types, files in subfolders) fails.
Layer 2, CONTENT: no file may match the sha256 of any local page code file (page_code_hashes.json; refresh with
tools/page_hashes.py), contain a local path, a key/token/secret pattern, or Street View / Google imagery references.

usage: python3 -I pipeline/tools/scan_commit.py            (scans `git diff --cached`)
       python3 -I pipeline/tools/scan_commit.py --check PATH_IN_REPO FILE   (judge FILE's bytes as if staged there)"""
import fnmatch, hashlib, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOW = [".gitignore", "README.md", "pipeline/README.md", "pipeline/POLICY.md", "pipeline/build_round.py",
         "pipeline/adapters/*.py", "pipeline/prompts/*.md", "pipeline/rounds/*.json",
         "pipeline/tools/*.py", "pipeline/tools/*.sh", "pipeline/tools/*.html", "pipeline/tools/page_code_hashes.json"]
SELF = {"pipeline/tools/scan_commit.py", "pipeline/tools/page_code_hashes.json"}   # hold the patterns/hashes
PATTERNS = [r"/Users/[^/\s]+", r"/private/(?:tmp|var)/", r"(?i)(api[_-]?key|secret|token|passphrase|password)\s*[=:]\s*\S{8,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY", r"(?i)streetview|maps\.googleapis|lh\d\.googleusercontent"]
HASHES = set(json.load(open(os.path.join(HERE, "page_code_hashes.json")))["hashes"])


def allowed(path):
    # fnmatch's * crosses "/", so also require the same folder depth as the pattern
    return any(fnmatch.fnmatchcase(path, p) and path.count("/") == p.count("/") for p in ALLOW)


def judge(path, blob):
    why = []
    if not allowed(path):
        why.append("not on the allowlist (only pipeline code, prompts, round files and docs may be committed)")
    if hashlib.sha256(blob).hexdigest() in HASHES:
        why.append("identical to local page code")
    if path not in SELF:
        text = blob.decode("utf-8", "replace")
        why += [f"matches {p}" for p in PATTERNS if re.search(p, text)]
    return why


def main():
    if sys.argv[1:2] == ["--check"]:
        why = judge(sys.argv[2], open(sys.argv[3], "rb").read())
        print("BLOCKED: " + "; ".join(why) if why else "ok")
        sys.exit(1 if why else 0)
    files = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z"],
                           capture_output=True, text=True, check=True).stdout.split("\0")
    files = [f for f in files if f]
    bad = []
    for f in files:
        blob = subprocess.run(["git", "show", f":{f}"], capture_output=True, check=True).stdout
        bad += [f"{f}: {w}" for w in judge(f, blob)]
    if bad:
        print("COMMIT BLOCKED:\n  " + "\n  ".join(bad))
        sys.exit(1)
    print(f"scan ok: {len(files)} staged files")


if __name__ == "__main__":
    main()
