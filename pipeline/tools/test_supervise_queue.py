#!/usr/bin/env python3
"""POST_R14X2 items 1-2: the supervisor's queue on a fake run folder (no real runs, no real irrigator).

1. a 'reasons' answer whose retry fails stays unapplied and is retried on the next pass;
2. 'reasons' on cl_gate re-runs cl_gate; the lead-gate approval item carries the under-deck clusters.
run: python3 -I pipeline/tools/test_supervise_queue.py
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = HERE


def main():
    tmp = tempfile.mkdtemp(prefix="sup_test_")
    runs, root, bin_ = (os.path.join(tmp, d) for d in ("runs", "root", "bin"))
    for d in (runs, root, bin_):
        os.makedirs(d)
    # a fake irrigator: fails while FAILS_LEFT > 0 (like "stop the controller before retrying a node"), logs every call
    calls, fails = os.path.join(tmp, "calls.txt"), os.path.join(tmp, "fails_left")
    open(fails, "w").write("1")
    with open(os.path.join(bin_, "irrigator"), "w") as f:
        f.write(f"""#!/bin/sh
echo "$@" >> '{calls}'
n=$(cat '{fails}'); if [ "$n" -gt 0 ]; then echo $((n-1)) > '{fails}'; echo "error: stop the controller before retrying a node"; exit 1; fi
echo "Prepared retry"
""")
    os.chmod(os.path.join(bin_, "irrigator"), 0o755)
    os.environ.update(PIPELINE_RUNS=runs, NYC_ROOT=root, PE_ROOT="", PATH=bin_ + os.pathsep + os.environ["PATH"])
    sys.path.insert(0, TOOLS)
    Dq = importlib.import_module("decisions")
    sup = importlib.import_module("supervise")

    rd = os.path.join(runs, "fake-1")
    os.makedirs(os.path.join(rd, "run"))
    json.dump({"round": "fake", "kind": "terrain_round_live"}, open(os.path.join(rd, "round.json"), "w"))
    json.dump({"tasks": {"cl_gate": {"status": "failed", "error": "check 0 (command): validator did not pass candidate"},
                         "lead_gate": {"status": "pending"}}}, open(os.path.join(rd, "run", "state.json"), "w"))
    sup.sync_queue(rd)
    it = next(i for i in Dq.items() if i["step"] == "cl_gate" and i["kind"] == "verdict")
    Dq.answer(it["id"], "reasons", "lead", "reasons added")

    sup.sync_queue(rd)   # pass 1: the retry fails -> unapplied, still answered
    it = Dq.load(it["id"])
    assert it["status"] == "answered" and not it.get("applied"), f"a failed retry must stay unapplied: {it}"
    sup.sync_queue(rd)   # pass 2: the retry works -> applied and closed
    it = Dq.load(it["id"])
    assert it.get("applied") and it["status"] == "closed", f"the second pass applies it: {it}"
    retried = [l for l in open(calls).read().splitlines() if l.startswith("retry")]
    assert len(retried) == 2 and all(l.endswith(" cl_gate") for l in retried), retried
    print("1. failed 'reasons' retry stays unapplied, applied on the next pass: ok")
    print("2a. 'reasons' on cl_gate re-runs cl_gate: ok")

    # 2b. the lead-gate approval item lists every under-deck cluster
    clusters = [{"tile": "x1_z2", "m2": 12.5, "deck_y_min": 7.1, "deck_over_b_min": 2.2, "x": 10, "z": 20},
                {"tile": "x3_z4", "m2": 3.25, "deck_y_min": 5.0, "deck_over_b_min": 1.1, "x": 30, "z": 40}]
    json.dump({"under_deck_clusters": clusters}, open(os.path.join(rd, "combined_layer.json"), "w"))
    json.dump({"tasks": {"cl_gate": {"status": "verified"},
                         "lead_gate": {"status": "failed", "error": "awaiting lead approval"}}},
              open(os.path.join(rd, "run", "state.json"), "w"))
    sup.sync_queue(rd)
    ap = next(i for i in Dq.items() if i["kind"] == "approval")
    ud = ap["evidence"]["under_deck"]
    assert ud["m2"] == 15.75 and len(ud["clusters"]) == 2 and ud["clusters"][1]["tile"] == "x3_z4", ud
    print("2b. lead-gate approval item carries the under-deck clusters: ok")

    # 2c. without combined_layer.json (a highway round) the item has no under_deck key
    rd2 = os.path.join(runs, "fake-2")
    os.makedirs(os.path.join(rd2, "run"))
    json.dump({"round": "fake2", "kind": "highway_round"}, open(os.path.join(rd2, "round.json"), "w"))
    json.dump({"tasks": {"lead_gate": {"status": "failed", "error": "awaiting lead approval"}}},
              open(os.path.join(rd2, "run", "state.json"), "w"))
    sup.sync_queue(rd2)
    ap2 = next(i for i in Dq.items() if i["run"] == "fake-2" and i["kind"] == "approval")
    assert "under_deck" not in ap2["evidence"], ap2["evidence"]
    print("2c. no combined_layer.json -> no under_deck block: ok")


if __name__ == "__main__":
    main()


def test_long_keys():
    """2026-11-27: ~200 stopped steps made the queue file name pass 255 bytes (Errno 63). The key is now short, the full
    list is in evidence["steps"], and a 'reasons' answer still retries every step."""
    import importlib, tempfile
    tmp = tempfile.mkdtemp(prefix="sup_long_")
    runs, bin_ = os.path.join(tmp, "runs"), os.path.join(tmp, "bin")
    os.makedirs(runs); os.makedirs(bin_)
    calls = os.path.join(tmp, "calls.txt")
    open(os.path.join(bin_, "irrigator"), "w").write(f"#!/bin/sh\necho \"$@\" >> '{calls}'\necho ok\n")
    os.chmod(os.path.join(bin_, "irrigator"), 0o755)
    os.environ.update(PIPELINE_RUNS=runs, PATH=bin_ + os.pathsep + os.environ["PATH"])
    for m in ("decisions", "supervise"):
        sys.modules.pop(m, None)
    Dq = importlib.import_module("decisions"); sup = importlib.import_module("supervise")
    rd = os.path.join(runs, "stations-full-b-2"); os.makedirs(os.path.join(rd, "run"))
    json.dump({"round": "s", "kind": "research_swarm"}, open(os.path.join(rd, "round.json"), "w"))
    steps = [f"{i}__photos" for i in range(100, 300)]
    json.dump({"tasks": {s: {"status": "uncertain", "error": "provider outcome uncertain (malformed_response)"} for s in steps}},
              open(os.path.join(rd, "run", "state.json"), "w"))
    sup.sync_queue(rd)
    it = next(i for i in Dq.items() if i["kind"] == "verdict")
    assert len(it["id"]) < 120 and it["step"].startswith("100__photos+199more-"), it["id"]
    assert it["evidence"]["steps"] == steps
    Dq.answer(it["id"], "reasons", "stations-coordinator")
    sup.sync_queue(rd)
    retried = [l.split()[-1] for l in open(calls).read().splitlines() if l.startswith("retry")]
    assert retried == steps, (len(retried), retried[:3])
    print("4. 200 stopped steps: short queue key, full list in evidence, 'reasons' retries all 200: ok")


if __name__ == "__main__":
    test_long_keys()
