#!/usr/bin/env python3
"""2026-11-27 (the lead, row 31): tests for p2_order_guard.py on a fake highways tree with tiny pass scripts.
1. replay runs assemble, passes, part_move, after passes and the CBE copy; two replays compare the same;
   a P2_ORDER_STABLE in the caller's env does not leak into a switch-off replay;
2. guard passes when every hooked pass is order-free; twins and their stat files are removed; the shared
   assemble_fdr.json is put back;
3. guard fails at the first hooked pass whose output follows the file order, and names the file.
run: python3 -I pipeline/tools/test_p2_order_guard.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="p2og_")
os.environ.update(NYC_ROOT=TMP, P2_GUARD_PY=sys.executable, P2_ORDER_STABLE="1")   # the last must not leak (test 1)
P2 = os.path.join(TMP, "highways", "work", "p2net")

HEAD = '''import json, os, sys, glob
D = sys.argv[1]; HW = os.path.join(os.environ["NYC_ROOT"], "highways")
def stat(name, v): json.dump(v, open(f"{HW}/work/p2net/{name}_{os.path.basename(D)}.json", "w"))
'''
HOOKED = HEAD + '''def _ord(L):
    L = sorted(L) if os.environ.get("P2_ORDER_STABLE") == "1" else list(L)
    if os.environ.get("P2_SHUFFLE_SEED"): import random as _r; _r.Random(int(os.environ["P2_SHUFFLE_SEED"])).shuffle(L)
    return L
'''
SCRIPTS = {
    "assemble_p2": '''import os, shutil, json
H = os.path.join(os.environ["NYC_ROOT"], "highways", "work"); out = os.environ["P2_OUT"]; os.makedirs(out)
for f in sorted(os.listdir(os.path.join(os.environ["NYC_ROOT"], "highways", os.environ["P2_BUILD"]))):
    shutil.copy(os.path.join(os.environ["NYC_ROOT"], "highways", os.environ["P2_BUILD"], f), out)
json.dump({"taken": "new"}, open(f"{H}/p2net/assemble_fdr.json", "w"))
''',
    "add_lane_attrs": "import sys\n",
    "plain": HEAD + 'open(f"{D}/plain.txt", "a").write(os.environ.get("P2_ORDER_STABLE", "off") + "\\n"); stat("plain", 1)\n',
    "stable": HOOKED + '''fs = _ord(glob.glob(f"{D}/t_*.txt"))   # order-free: the output is sorted whatever the read order
open(f"{D}/sum.txt", "w").write("".join(sorted(open(f).read() for f in fs))); stat("stable", len(fs))
''',
    "unstable": HOOKED + '''fs = _ord(sorted(glob.glob(f"{D}/t_*.txt")))   # follows the read order: the shuffle changes the bytes
open(f"{D}/order.txt", "w").write(" ".join(os.path.basename(f) for f in fs)); stat("unstable", len(fs))
''',
    "light": HEAD + 'json.dump([], open(f"{HW}/work/p2net/lightwell_{os.path.basename(D)}.json", "w"))\n',
    "part_move": HEAD + 'assert os.environ["KEEP_UP"].endswith(f"lightwell_{os.path.basename(D)}.json"); open(f"{D}/moved.txt", "w").write(os.environ["UP"])\n',
    "tailpass": HEAD + 'open(f"{D}/tail.txt", "w").write(os.environ["COVERED"])\n',
}


def setup():
    os.makedirs(P2)
    for k, v in SCRIPTS.items():
        open(os.path.join(P2, f"{k}.py"), "w").write(v)
    b = os.path.join(P2, "b_test", "all")
    os.makedirs(b)
    for i in range(8):
        open(os.path.join(b, f"t_{i}.txt"), "w").write(f"{i}\n")
    spec = importlib.util.spec_from_file_location("p2og", os.path.join(HERE, "p2_order_guard.py"))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    cbe = os.path.join(TMP, "highways", "work", "hw5", "b9", "all")
    os.makedirs(cbe)
    for t in g.CBE:
        for f in (f"tile_{t}_above.glb", f"lanes_{t}.json"):
            open(os.path.join(cbe, f), "w").write(t)
    open(os.path.join(P2, "assemble_fdr.json"), "w").write('{"taken": "old"}')
    return g


def cfg(passes):
    p = os.path.join(TMP, f"round_{'_'.join(passes)}.json")
    json.dump({"round": "rt", "p2_terv": "v", "input_build": "work/p2net/b_test", "passes": passes,
               "pass_env": {"COVERED": "1,2"}, "part_move_up": "9", "after_part_move": ["tailpass"]}, open(p, "w"))
    return p


def t_replay(g):
    c = cfg(["plain", "light", "unstable"])
    assert g.main(["replay", c, "work/r1"]) == 0 and g.main(["replay", c, "work/r2"]) == 0
    R = os.path.join(TMP, "highways", "work", "r1")
    assert open(f"{R}/plain.txt").read() == "off\n", "the caller's P2_ORDER_STABLE leaked into a switch-off replay"
    assert open(f"{R}/moved.txt").read() == "9" and open(f"{R}/tail.txt").read() == "1,2"
    assert os.path.exists(f"{R}/tile_{g.CBE[0]}_above.glb") and os.path.exists(f"{R}/lanes_{g.CBE[-1]}.json")
    assert g.main(["compare", "work/r1", "work/r2"]) == 0
    assert json.load(open(os.path.join(P2, "assemble_fdr.json"))) == {"taken": "old"}, "shared assemble record not put back"
    print("1. replay: chain, part_move env, after pass, CBE copy, no env leak, shared record kept: ok")


def t_guard_pass(g):
    rc = g.main(["guard", cfg(["plain", "stable", "light"]), "work/g1", "--seeds", "1,2"])
    rep = json.load(open(os.path.join(TMP, "highways", "work", "g1.guard.json")))
    assert rc == 0 and rep["pass"], rep
    assert [p["pass"] for p in rep["passes"] if p["hooked"]] == ["stable", "stable"], rep
    left = [f for f in os.listdir(os.path.join(TMP, "highways", "work")) if f.startswith("g1.")]
    assert left == ["g1.guard.json"] or sorted(left) == ["g1.guard.json", "g1.logs"], left
    assert not [f for f in os.listdir(P2) if ".seed" in f], "twin stat files left behind"
    assert open(os.path.join(TMP, "highways", "work", "g1", "plain.txt")).read() == "1\n", "guard runs switch on"
    print("2. guard: order-free hooked pass same under 2 seeds; twins and their stats removed: ok")


def t_guard_fail(g):
    rc = g.main(["guard", cfg(["plain", "unstable", "stable"]), "work/g2"])
    rep = json.load(open(os.path.join(TMP, "highways", "work", "g2.guard.json")))
    assert rc == 1 and rep["pass"] is False, rep
    last = rep["passes"][-1]
    assert last["pass"] == "unstable" and last["differ"] == ["order.txt"], last
    assert "stable" not in [p["pass"] for p in rep["passes"]], "guard went on past the first difference"
    print("3. guard: order-following pass caught (order.txt), stops there: ok")


if __name__ == "__main__":
    g = setup()
    t_replay(g)
    t_guard_pass(g)
    t_guard_fail(g)
    print("all p2_order_guard tests pass")
