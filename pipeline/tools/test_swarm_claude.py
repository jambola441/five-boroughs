#!/usr/bin/env python3
"""2026-11-28: tests for adapters/swarm_claude.py (station rounds on Claude Sonnet with Street View, owner's OK).
1. the claude -p command: --restricted, no settings or MCP, no transcript, dontAsk; writes only the item's files and
   plan*.json; Bash only ./capture.sh and ./view.sh; every deny_read path denied to Read; read_dirs added; USD cap per part;
2. agent_claude_ws: files copied into the step folder and back, the folder removed, cost and denials reported;
3. main refuses a round whose engine is not claude_ws.
run: python3 -I pipeline/tools/test_swarm_claude.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="swcl_")
os.environ.update(NYC_ROOT=TMP, PIPELINE_OUT=os.path.join(TMP, "run_out"))
sys.path.insert(0, os.path.join(HERE, "..", "adapters"))
import swarm_claude as C   # noqa: E402

CFG = {"engine": "claude_ws", "root": "st", "read_dirs": ["st", "data"], "deny_read": ["st/truth"],
       "main": {"writes": ["cards/cx_{id}.json"], "budget_usd": 7}, "helpers": [{"id": "photos", "budget_usd": 3}]}


def t_command():
    cmd = C.command(CFG, "9__main", "PROMPT", "claude-sonnet-5-5", {"type": "object"}, ["cards/cx_9.json"])
    for f in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert f in cmd, f
    assert cmd[cmd.index("--setting-sources") + 1] == "" and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5" and cmd[-1] == "PROMPT"
    i, j = cmd.index("--allowedTools"), cmd.index("--disallowedTools")
    allow, deny = cmd[i + 1:j], cmd[j + 1:cmd.index("--add-dir")]
    writes = sorted(a for a in allow if a.startswith(("Write(", "Edit(")))
    assert writes == ["Edit(./cx_9.json)", "Edit(./plan*.json)", "Write(./cx_9.json)", "Write(./plan*.json)"], writes
    assert sorted(a for a in allow if a.startswith("Bash")) == ["Bash(./capture.sh:*)", "Bash(./view.sh:*)"], allow
    assert "Bash" not in allow, "bare Bash would allow every command"
    p = os.path.join(TMP, "st", "truth")
    assert deny == [f"Read(/{p}/**)", f"Read(/{p})"], deny
    adds = [cmd[k + 1] for k, x in enumerate(cmd) if x == "--add-dir"]
    assert adds == [os.path.join(TMP, "st"), os.path.join(TMP, "data")], adds
    assert cmd[cmd.index("--max-budget-usd") + 1] == "7"
    h = C.command(CFG, "9__photos", "P", "m", {}, [])
    assert h[h.index("--max-budget-usd") + 1] == "3"
    assert not [a for a in h if a.startswith("Write(./cx")], "a helper may write only plan files"
    print("1. command: restricted, writes and Bash limited, deny_read denied, read_dirs added, per-part caps: ok")


def t_agent():
    root = os.path.join(TMP, "st")
    os.makedirs(os.path.join(root, "cards"))
    open(os.path.join(root, "cards", "cx_9.json"), "w").write('{"v": 0}')
    seen = {}

    def fake_run_job(name, cmd, cwd, log, cores=None, check_load=True):
        seen.update(name=name, cwd=cwd, cores=cores, check_load=check_load, had=open(os.path.join(cwd, "cx_9.json")).read())
        open(os.path.join(cwd, "cx_9.json"), "w").write('{"v": 1}')
        d = os.path.join(C.OUT, "jobs", name)
        os.makedirs(d)
        json.dump({"started": 0}, open(os.path.join(d, "job.json"), "w"))
        open(os.path.join(d, "rc"), "w").write("0")
        json.dump({"structured_output": {"card": "x"}, "total_cost_usd": 1.25, "usage": {"input_tokens": 10, "output_tokens": 5},
                   "permission_denials": [{"tool_name": "Bash"}]}, open(os.path.join(C.OUT, log), "w"))
        return 0
    C.S.run_job = fake_run_job
    out, cost, mins, denied = C.agent_claude_ws("9__main", "P", CFG, "m", {}, files=["cards/cx_9.json"])
    assert out == {"card": "x"} and cost == 1.25 and denied == 1 and C.S.TOKENS["9__main"] == 15
    assert seen["had"] == '{"v": 0}' and seen["cores"] == 2 and seen["check_load"] is False, seen
    assert seen["cwd"].startswith(C.WS + "/") and not os.path.exists(seen["cwd"]), "step folder not removed"
    assert open(os.path.join(root, "cards", "cx_9.json")).read() == '{"v": 1}', "card not copied back"
    print("2. agent: card copied in and back, folder removed, cost, tokens and denials reported: ok")


def t_engine():
    C.load_request = lambda: {"task": {"id": "9__main"}}
    C.round_config = lambda: {**CFG, "engine": "agy"}
    try:
        C.main()
    except RuntimeError as e:
        assert "claude_ws" in str(e)
        print("3. main: refuses a round that is not claude_ws: ok")
        return
    raise AssertionError("main ran a non-claude_ws round")


if __name__ == "__main__":
    t_command()
    t_agent()
    t_engine()
    print("all swarm_claude tests pass")
