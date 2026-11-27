"""Cap concurrent agy calls in swarm rounds (owner's OK, 2026-11-22, after the stadium pilot reached load ~60).

Each ready step starts its agy call as a detached job and returns, and Irrigator's --workers caps adapter calls, not
jobs, so dozens of calls could start at once. Here every agy job starts with cores=cfg["agy_cores"] (default 1), so
common.heavy_slot lets it start only while the heavy jobs in the same runs folder stay under HEAVY_CORES (not gated on
the load average since 2026-11-27: agy is ~1 % CPU, and the load test was tripped by the retry churn it caused); otherwise the step reports "job running: waiting for a heavy-job slot" and is retried.
Used by swarm_ext.py and swarm_plan.py; swarm.py is unchanged (pinned by running rounds).
"""
import os


def wrap(S, cfg):
    if getattr(S.run_job, "_slots", False):
        return
    orig = S.run_job
    n = int(cfg.get("agy_cores", 1))

    def run_job(name, cmd, cwd, log, env=None, wait=None, cores=None, **k):
        if cmd and os.path.basename(str(cmd[0])) == "agy":
            if cores is None:
                cores = n
            k.setdefault("check_load", False)   # 2026-11-27: the budget limits agy jobs; load average doesn't
        return orig(name, cmd, cwd, log, env=env, wait=wait, cores=cores, **k)
    run_job._slots = True
    S.run_job = run_job
