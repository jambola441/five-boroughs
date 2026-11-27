#!/usr/bin/env python3
"""2026-11-27 (owner: "yes both"): the retry churn fix.
1. heavy_slot(check_load=False) ignores the load average but keeps the HEAVY_CORES budget;
2. agy_slots passes check_load=False for agy jobs only;
3. supervise.plan re-tries at most HEAVY_RETRY_PER_PASS steps waiting for a heavy-job slot per pass (all other waiting
   kinds as before).
run: python3 -I pipeline/tools/test_load_churn.py
"""
import importlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="churn_")
os.environ.update(PIPELINE_RUNS=os.path.join(TMP, "runs"), NYC_ROOT=TMP, PE_ROOT="", PIPELINE_OUT=os.path.join(TMP, "runs", "r"))
os.makedirs(os.environ["PIPELINE_OUT"])
sys.path.insert(0, os.path.join(HERE, "..", "adapters"))
sys.path.insert(0, HERE)
import common   # noqa: E402
import agy_slots   # noqa: E402


def t_heavy():
    runs = os.environ["PIPELINE_RUNS"]
    assert common.heavy_slot(1, runs, ncpu=15, load=40.0) is not None, "the load test still applies by default"
    assert common.heavy_slot(1, runs, ncpu=15, load=40.0, check_load=False) is None, "agy jobs skip the load test"
    for i in range(common.HEAVY_CORES):   # fill the budget with running heavy jobs
        d = os.path.join(runs, "r", "jobs", f"j{i}")
        os.makedirs(d)
        json.dump({"pid": os.getpid(), "cores": 1, "started": time.time()}, open(os.path.join(d, "job.json"), "w"))
    why = common.heavy_slot(1, runs, ncpu=15, load=0.0, check_load=False)
    assert why and "heavy cores in use" in why, why
    print("1. heavy_slot: load test skippable, HEAVY_CORES budget kept: ok")


def t_slots():
    seen = []

    class S:
        @staticmethod
        def run_job(name, cmd, cwd, log, env=None, wait=None, cores=None, **k):
            seen.append((cmd[0], cores, k.get("check_load", True)))
    agy_slots.wrap(S, {})
    S.run_job("a", ["agy", "-p", "x"], ".", "l")
    S.run_job("b", ["/usr/bin/python3", "x.py"], ".", "l")
    assert seen == [("agy", 1, False), ("/usr/bin/python3", None, True)], seen
    print("2. agy_slots: agy jobs cores=1 and no load test; other jobs unchanged: ok")


def t_supervise():
    sup = importlib.import_module("supervise")
    rd = os.path.join(os.environ["PIPELINE_RUNS"], "swarm-1")
    os.makedirs(os.path.join(rd, "run"))
    json.dump({"round": "s", "kind": "research_swarm"}, open(os.path.join(rd, "round.json"), "w"))
    tasks = {f"{i}__photos": {"status": "failed", "error": "job running: waiting for a heavy-job slot for x: 12 of 12 heavy cores in use"}
             for i in range(50)}
    tasks.update({f"r{i}__main": {"status": "failed", "error": f"job running: r{i}__main, 3 min so far"} for i in range(3)})
    tasks["done"] = {"status": "verified"}
    json.dump({"tasks": tasks}, open(os.path.join(rd, "run", "state.json"), "w"))
    todo = sup.plan(rd)
    heavy = [t for t in todo if "heavy-job slot" in t[2]]
    running = [t for t in todo if t[2] == "job running"]
    assert len(heavy) == sup.HEAVY_RETRY_PER_PASS == 6 and len(running) == 3, (len(heavy), len(running))
    print(f"3. supervise.plan: 50 parked steps -> {len(heavy)} re-tried per pass; 3 running jobs still re-tried: ok")


if __name__ == "__main__":
    t_heavy()
    t_slots()
    t_supervise()
    print("all churn tests pass")
