#!/usr/bin/env python3
"""POST_R14X2 items 7-8 on a fake terrain-study tree (no real round is frozen, nothing real is chmod'ed).

7. t_freeze: every file the round's index sends the page to (tiles by 'v', water by 'water_v', the overview) goes into
   a refs manifest; v1 must match the lead's v1 pin exactly; v1 is made read-only only in a live freeze.
8. gate_seam: both seam files pinned by sha; exit 0 PASS, 1 FAIL, 2 bad input (fails closed); a missing pin stops it.
run: python3 -I pipeline/tools/test_terrain_freeze.py
     (with NYC_ROOT set, also: the real v1c_r6t2 index resolves to 1,627 tiles in v1, read-only, nothing written there)
"""
import hashlib
import json
import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_ROOT = os.environ.get("NYC_ROOT", "")
TMP = tempfile.mkdtemp(prefix="terrain_test_")
os.environ.update(NYC_ROOT=TMP, PIPELINE_OUT=os.path.join(TMP, "out"))
os.makedirs(os.environ["PIPELINE_OUT"])
sys.path.insert(0, os.path.join(HERE, "..", "adapters"))
sys.path.insert(0, os.path.join(HERE, ".."))
import round_live as RL   # noqa: E402
import common   # noqa: E402
import build_round as BR   # noqa: E402

TS = os.path.join(TMP, "work/terrain_study")
TER = os.path.join(TS, "p1/terrain")
H = lambda b: hashlib.sha256(b).hexdigest()


def write(p, b):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b)


def tree():
    """v1 (all tiles), v1b (one tile), the round (one tile + water), and the base folder the freeze also checks."""
    tiles = {"tile_x0_z0": {"v": "v1c", "water_file": True, "water_v": "v1c"},
             "tile_x1_z0": {"v": "v1b"},
             "tile_x0_z1": {"water_file": True}}
    for t in tiles:
        for l in range(4):
            write(f"{TER}/v1/tiles/{t}_l{l}.bin", f"v1 {t} {l}".encode())
        write(f"{TER}/v1/water/{t}_water.bin", f"v1 water {t}".encode())
    write(f"{TER}/v1/overview_64m.bin", b"overview")
    write(f"{TER}/v1/index.json", b"{}")
    for l in range(4):
        write(f"{TER}/v1b/tiles/tile_x1_z0_l{l}.bin", f"v1b {l}".encode())
        write(f"{TER}/v1c_rt/tiles/tile_x0_z0_l{l}.bin", f"rt {l}".encode())
    write(f"{TER}/v1c_rt/water/tile_x0_z0_water.bin", b"rt water")
    write(f"{TER}/v1c_rt/index.json", json.dumps({"lods": [{}, {}, {}, {}], "tiles": tiles,
                                                  "overview": {"file": "overview_64m.bin"}}).encode())
    write(f"{TS}/p1/bases_h16_t1c_rt/base.bin", b"base")
    for loc, man in (("p1/terrain/v1c_rt", "p2/out/manifest_terrain_v1c_rt_sha256.json"),
                     ("p1/bases_h16_t1c_rt", "p2/out/manifest_bases_h16_t1c_rt_sha256.json")):
        d = os.path.join(TS, loc)
        m = {os.path.relpath(os.path.join(a, f), d): H(open(os.path.join(a, f), "rb").read()) for a, _, fs in os.walk(d) for f in fs}
        write(os.path.join(TS, man), json.dumps(m).encode())
    pin = "".join(f"{H(open(os.path.join(a, f), 'rb').read())}  ./{os.path.relpath(os.path.join(a, f), TER + '/v1')}\n"
                  for a, _, fs in sorted(os.walk(TER + "/v1")) for f in sorted(fs))
    write(f"{TMP}/work/lead_reviews/pin.txt", pin.encode())
    return {"file": "work/lead_reviews/pin.txt", "sha12": H(pin.encode())[:12], "files": pin.count("\n")}


def unlock(p):
    for a, ds, fs in os.walk(p):
        os.chmod(a, 0o755)
        for f in fs:
            os.chmod(os.path.join(a, f), 0o644)


def t_freeze():
    pin = tree()
    cfg = {"round": "v1c_rt", "mode": "existing", "v1_pin": pin}
    out, err = RL.t_freeze(cfg)
    assert out and not err, err
    by = json.loads(out["refs_outside"])
    # v1: tile_x0_z1's 4 LoDs + its water + the overview; v1b: tile_x1_z0's 4 LoDs; the round's own are in its manifest
    assert by == {"v1": 6, "v1b": 4}, by
    refs = json.load(open(os.path.join(os.environ["PIPELINE_OUT"], "manifest_index_refs_v1c_rt_sha256.json")))
    assert "v1/water/tile_x0_z1_water.bin" in refs and "v1/overview_64m.bin" in refs and "v1b/tiles/tile_x1_z0_l3.bin" in refs
    assert os.access(f"{TER}/v1/tiles/tile_x0_z1_l0.bin", os.W_OK), "existing mode never chmods"
    print("7a. refs manifest: tiles by v, water by water_v, the overview; v1 matches the pin: ok")

    write(f"{TER}/v1/tiles/tile_x0_z1_l2.bin", b"changed")
    out, err = RL.t_freeze(cfg)
    assert out is None and "don't match the lead's pin" in err, err
    pin2 = tree()
    out, err = RL.t_freeze({**cfg, "v1_pin": {**pin2, "sha12": "000000000000"}})
    assert out is None and "isn't the lead's" in err, err
    os.remove(f"{TER}/v1b/tiles/tile_x1_z0_l1.bin")
    out, err = RL.t_freeze({**cfg, "v1_pin": pin2})
    assert out is None and "doesn't exist" in err, err
    print("7b. a changed v1 file, a wrong pin file, a missing referenced file: each stops the freeze: ok")

    pin3 = tree()
    out, err = RL.t_freeze({**cfg, "mode": "live", "v1_pin": pin3})
    assert out and not err, err
    mode = os.stat(f"{TER}/v1/tiles/tile_x0_z1_l0.bin").st_mode
    assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), "a live freeze makes v1 read-only"
    for p in (f"{TER}/v1", f"{TER}/v1c_rt", f"{TS}/p1/bases_h16_t1c_rt"):
        unlock(p)
    print("7c. a live freeze makes v1 (and the round) read-only: ok")


def t_seam():
    venv = os.path.join(TMP, ".venv/bin")
    os.makedirs(venv, exist_ok=True)
    os.symlink(sys.executable, os.path.join(venv, "python"))
    RL.PY = os.path.join(venv, "python")
    script = (b"import json, os, sys\nrc = int(open(os.path.join(os.path.dirname(__file__), 'rc')).read())\n"
              b"ST = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))\n"
              b"os.makedirs(os.path.join(ST, 'p2/checks'), exist_ok=True)\n"
              b"s = dict(PASS=rc == 0, fails=0 if rc == 0 else 7, mismatches=9, by_lod={'L0': {'FAIL_new': 7}})\n"
              b"rc == 2 or json.dump({'summary': s}, open(os.path.join(ST, 'p2/checks', os.environ['R6N'] + '_seam_check_v2.json'), 'w'))\n"
              b"assert os.environ['PREV'] == 'v1c_prev' and os.environ['CONFIG'].endswith('seam_check_config.json')\n"
              b"sys.exit(rc)\n")
    write(f"{TS}/p2/tools/seam_check_v2.py", script)
    write(f"{TS}/p2/tools/seam_check_config.json", b'{"tol_m": 0.005, "cap_m": 0.5}')
    pins = {"seam_check_v2.py": H(script)[:8], "seam_check_config.json": H(b'{"tol_m": 0.005, "cap_m": 0.5}')[:8]}
    cfg = {"round": "v1c_rt", "prev": "v1c_prev", "mode": "live", "seam_check": {"scripts_sha": pins}}
    jobs = os.path.join(os.environ["PIPELINE_OUT"], "jobs")

    def run(rc):
        open(f"{TS}/p2/tools/rc", "w").write(str(rc))
        if os.path.isdir(os.path.join(jobs, "gate_seam")):
            for f in os.listdir(os.path.join(jobs, "gate_seam")):
                os.remove(os.path.join(jobs, "gate_seam", f))
            os.rmdir(os.path.join(jobs, "gate_seam"))
        for _ in range(120):
            try:
                return RL.seam_gate(cfg)
            except common.Running:
                time.sleep(0.5)
        raise AssertionError("seam job never finished")
    out, err = run(0)
    assert out["pass"] == "true" and "seam PASS" in out["detail"], (out, err)
    out, err = run(1)
    assert out["pass"] == "false" and "seam FAIL: 7" in out["detail"], out
    out, err = run(2)
    assert out["pass"] == "false" and "bad input" in out["detail"] and "fails closed" in out["detail"], out
    out, err = run(3)
    assert out["pass"] == "false" and "fails closed" in out["detail"], out
    out, err = RL.seam_gate({**cfg, "seam_check": {"scripts_sha": {"seam_check_v2.py": pins["seam_check_v2.py"]}}})
    assert out is None and "no pin for seam_check_config.json" in err, err
    out, err = RL.seam_gate({**cfg, "seam_check": {"scripts_sha": {**pins, "seam_check_v2.py": "deadbeef"}}})
    assert out is None and "pinned deadbeef" in err, err
    print("8a. gate_seam: exit 0 PASS, 1 FAIL, 2 bad input and others fail closed; unpinned or changed files stop it: ok")
    plan = BR.terrain_live_tasks([], {"seam_check": {"scripts_sha": pins}})
    ids = {t["id"]: t for t in plan}
    acc_after = ids["qa_accept"].get("depends_on") or ids["qa_accept"].get("after") or []
    assert "gate_seam" in ids and "gate_seam" in acc_after, acc_after
    assert "gate_seam" not in {t["id"] for t in BR.terrain_live_tasks([], {})}
    print("8b. a round file with seam_check gets gate_seam before qa_accept; without it, no gate_seam: ok")


def t_real():
    if not REAL_ROOT or not os.path.exists(os.path.join(REAL_ROOT, "work/terrain_study/p1/terrain/v1c_r6t2/index.json")):
        return
    common_root = os.environ["NYC_ROOT"]
    os.environ["NYC_ROOT"] = REAL_ROOT
    try:
        import importlib
        importlib.reload(common)
        RL.project = common.project
        refs, err = RL.index_refs("v1c_r6t2")
        assert not err, err
        tiles_v1 = {r.split("/")[2].rsplit("_l", 1)[0] for r in refs if r.startswith("v1/tiles/")}
        assert len(tiles_v1) == 1627, len(tiles_v1)
        assert all(os.path.exists(os.path.join(REAL_ROOT, "work/terrain_study/p1/terrain", r)) for r in refs)
        print(f"7d. the real v1c_r6t2 index: {len(refs)} referenced files, 1,627 tiles in v1, all present (read-only): ok")
    finally:
        os.environ["NYC_ROOT"] = common_root
        importlib.reload(common)
        RL.project = common.project


if __name__ == "__main__":
    try:
        t_freeze()
        t_seam()
        t_real()
    finally:
        for p in (f"{TER}/v1", f"{TER}/v1c_rt", f"{TS}/p1/bases_h16_t1c_rt"):
            if os.path.isdir(p):
                unlock(p)
    print("all terrain freeze tests pass")
