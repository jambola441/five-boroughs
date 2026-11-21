#!/usr/bin/env python3
"""Decision queue: rounds that wait on a person raise ONE item here; the owner PULLS it when free and answers it.
No decision travels as a message, so two sessions can't act on different copies of one decision.

  decisions.py list [--owner OWNER] [--all]     open items (oldest first; --all includes answered / closed)
  decisions.py show ID                          one item with its evidence
  decisions.py answer ID CHOICE --by NAME [--note TEXT]

Owners (POLICY.md): lead · page-coordinator · macmini · terrain-study · page-editor · drive-test · pipeline.
Kinds:
  approval  the lead gate. approve -> the supervisor writes the run's approval.json; reject -> the run is retired.
  verdict   a round stopped at a gate. Its stream's owner answers: fix (a new round; this run is retired),
            reasons (the check's owner added reasons; the supervisor re-runs it), debt (asks the lead: a new
            'debt' item for the lead is raised), hold.
  debt      the lead's call on a debt request: approve (the pipeline engineer wires it) / reject.
  frames    the page editor renders frames; closes by itself when READY appears.
  task      work for the pipeline engineer (e.g. wire an approved debt); answered 'done' when done.
An item is tied to the round.json version it was raised on. If the round changes before the answer is applied,
the item turns 'stale' and a fresh one is raised. Items live in $PIPELINE_RUNS/_queue (local, never in the repo).
"""
import argparse
import hashlib
import json
import os
import sys
import time

RUNS = os.environ.get("PIPELINE_RUNS", os.path.expanduser("~/five-boroughs-runs"))
Q = os.path.join(RUNS, "_queue")
CHOICES = {"approval": ["approve", "reject"], "verdict": ["fix", "reasons", "debt", "hold"],
           "debt": ["approve", "reject"], "frames": [], "task": ["done"]}


def _p(i):
    return os.path.join(Q, i + ".json")


def version(run_dir):
    return hashlib.sha256(open(os.path.join(run_dir, "round.json"), "rb").read()).hexdigest()[:12]


def load(i):
    return json.load(open(_p(i)))


def save(it):
    os.makedirs(Q, exist_ok=True)
    tmp = _p(it["id"]) + ".tmp"
    json.dump(it, open(tmp, "w"), indent=1, ensure_ascii=False)
    os.replace(tmp, _p(it["id"]))


def items():
    if not os.path.isdir(Q):
        return []
    return [json.load(open(os.path.join(Q, f))) for f in sorted(os.listdir(Q)) if f.endswith(".json")]


def raise_item(run, step, kind, owner, question, evidence, run_dir, priority=2):
    """Raise once per (run, step, kind). A changed round.json makes an unanswered item stale and raises a new one."""
    v = version(run_dir)
    base = f"{run}--{step}--{kind}"
    for it in items():
        if it["key"] == base and it["status"] in ("open", "answered"):
            if it["version"] == v or it["status"] == "answered":
                return None
            it["status"], it["closed_why"] = "stale", "the round changed before an answer was applied"
            save(it)
    i = f"{base}--{v}"
    if os.path.exists(_p(i)):
        return None
    save({"id": i, "key": base, "run": run, "step": step, "kind": kind, "owner": owner, "question": question,
          "choices": CHOICES[kind], "evidence": evidence, "priority": priority, "version": v, "status": "open",
          "raised_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return i


def close(it, why):
    it["status"], it["closed_why"], it["closed_at"] = "closed", why, time.strftime("%Y-%m-%d %H:%M:%S")
    save(it)


def answer(i, choice, by, note=""):
    it = load(i)
    if it["status"] != "open":
        sys.exit(f"{i} is {it['status']}, not open")
    if choice not in it["choices"]:
        sys.exit(f"choice must be one of {it['choices']}")
    it.update(status="answered", answer={"choice": choice, "by": by, "note": note,
                                         "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    save(it)
    return it


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("list"); a.add_argument("--owner"); a.add_argument("--all", action="store_true")
    a = sub.add_parser("show"); a.add_argument("id")
    a = sub.add_parser("answer"); a.add_argument("id"); a.add_argument("choice"); a.add_argument("--by", required=True)
    a.add_argument("--note", default="")
    x = ap.parse_args()
    if x.cmd == "list":
        rows = [it for it in items() if (x.all or it["status"] == "open") and (not x.owner or it["owner"] == x.owner)]
        rows.sort(key=lambda it: (it["priority"], it["raised_at"]))
        for it in rows:
            print(f"{it['id']}  [{it['status']}] owner={it['owner']}  {it['kind']}: {it['question']}"
                  + (f"  choices={'/'.join(it['choices'])}" if it["choices"] else ""))
        if not rows:
            print("nothing waiting" + (f" for {x.owner}" if x.owner else ""))
    elif x.cmd == "show":
        print(json.dumps(load(x.id), indent=1, ensure_ascii=False))
    else:
        it = answer(x.id, x.choice, x.by, x.note)
        print(f"answered {it['id']}: {x.choice} (the supervisor applies it within a minute)")


if __name__ == "__main__":
    main()
