"""Delete each agy call's own conversation folder after the step (owner's OK, 2026-11-22).

agy keeps every conversation under ~/.gemini/antigravity-cli/brain/<conversation_id>/: the transcript, a copy of every
image the agent viewed or generated, and browser recordings. That outlives the turn's scratch folder, against the rule
that fetched photos and Street View stay in the scratch folder only. After a step's job has finished and its log is
saved in the run folder (agents/<name>.json), this removes exactly the conversation ids named in that log, nothing else.
Used by swarm_ext.py and swarm_plan.py; swarm.py is unchanged (pinned by running rounds).
"""
import os
import re
import shutil
import time

from common import OUT

BRAIN = os.environ.get("AGY_BRAIN_DIR") or os.path.expanduser("~/.gemini/antigravity-cli/brain")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def cleanup(name):
    if not os.path.exists(os.path.join(OUT, "jobs", name, "rc")):
        return   # the job is still running (or never started): its folder is in use
    try:
        text = open(os.path.join(OUT, "agents", f"{name}.json")).read()
    except OSError:
        return
    root = os.path.realpath(BRAIN)
    for cid in sorted(set(re.findall(r'"conversation_id"\s*:\s*"([^"]+)"', text))):
        d = os.path.join(BRAIN, cid)
        if not UUID.match(cid) or not os.path.isdir(d) or os.path.dirname(os.path.realpath(d)) != root:
            continue
        size = sum(os.path.getsize(os.path.join(p, f)) for p, _, fs in os.walk(d) for f in fs)
        shutil.rmtree(d, ignore_errors=True)
        with open(os.path.join(OUT, "agy_cleanup.log"), "a") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name} removed brain/{cid} ({size} bytes)\n")


def wrap(S):
    """Make swarm.agent_agy clean up after itself (swarm.agent looks the name up at call time)."""
    if getattr(S.agent_agy, "_cleans", False):
        return
    orig = S.agent_agy

    def agent_agy(name, *a, **k):
        try:
            return orig(name, *a, **k)
        finally:
            cleanup(name)
    agent_agy._cleans = True
    S.agent_agy = agent_agy
