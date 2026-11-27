#!/usr/bin/env python3
"""P2 order-stability guard (register row 31, the lead 2026-11-27). A standing check tool: rounds never import it.

The highway passes that read files or union geometry in an order the filesystem or GEOS chooses carry a switch,
P2_ORDER_STABLE=1 (sorted globs, sorted union inputs, geometric tie-breaks, canonical ring serialization at write),
and a hook, P2_SHUFFLE_SEED=<n>, that shuffles their file lists. Switch off, a pass is byte-identical to the frozen
chain. Switch on, its output must not depend on the order, so a shuffled run must give the same bytes.

  replay ROUND_JSON OUT_DIR [--stable 0|1]
      Replays the round's chain on a test copy as adapters/highway.py runs it (assemble, passes in order with the
      round's pass_env, part_move, after_part_move, CBE copy), into OUT_DIR (under highways/, must not exist).
  compare A B
      Byte-compares every file under two round folders; exits 1 on any difference.
  guard ROUND_JSON WORK_DIR [--seeds 1,2]
      Replays the chain with the switch on; every pass whose script has the shuffle hook runs again from the same
      input once per seed, with P2_SHUFFLE_SEED; the round folder's files must be byte-identical with and without
      it. Writes WORK_DIR.guard.json and exits 1 at the first pass that differs.

Each script's own <pass>_<folder>.json goes to highways/work/p2net as in a round, named after OUT_DIR's folder; the
guard's twin folders and their stat files are removed after each comparison. assemble_p2.py rewrites the shared
work/p2net/assemble_<stretch>.json: the tool keeps the existing copy and puts it back. Nothing is uploaded or pushed.
Env: NYC_ROOT (default ~/Desktop/nyc-buildings), P2_GUARD_PY (default NYC_ROOT/.venv/bin/python).
run: python3 -I pipeline/tools/p2_order_guard.py guard pipeline/rounds/r14x4.json work/p2net/design_trench_cut/g1
"""
import filecmp
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "adapters"))
from highway import CBE   # noqa: E402  (the adapter's own CBE tile list)

ROOT = os.environ.get("NYC_ROOT") or os.path.expanduser("~/Desktop/nyc-buildings")
HW = os.path.join(ROOT, "highways")
P2 = os.path.join(HW, "work", "p2net")
PY = os.environ.get("P2_GUARD_PY") or os.path.join(ROOT, ".venv", "bin", "python")
HOOK = "P2_SHUFFLE_SEED"
STRIP = ("P2_ORDER_STABLE", HOOK)


def hw(p):
    return p if os.path.isabs(p) else os.path.join(HW, p)


def run(argv, log, env):
    e = {k: v for k, v in os.environ.items() if k not in STRIP}
    e.update(env)
    with open(log, "w") as fh:
        rc = subprocess.call(argv, cwd=HW, env=e, stdout=fh, stderr=subprocess.STDOUT)
    if rc:
        raise SystemExit(f"{os.path.basename(log)}: exited {rc} (see {log})")


def hooked(script):
    return HOOK in open(os.path.join(P2, f"{script}.py")).read()


def files(d):
    return sorted(os.path.relpath(os.path.join(a, f), d) for a, _, fs in os.walk(d) for f in fs)


def compare(a, b):
    fa, fb = set(files(a)), set(files(b))
    differ = [f for f in sorted(fa & fb) if not filecmp.cmp(os.path.join(a, f), os.path.join(b, f), shallow=False)]
    return {"files": len(fa | fb), "only_a": sorted(fa - fb), "only_b": sorted(fb - fa), "differ": differ,
            "same": not differ and fa == fb}


def stat_files(name):
    """The per-pass stat files a script writes for a round folder named <name> (work/p2net/*_<name>.json)."""
    return set(glob.glob(os.path.join(P2, f"*_{name}.json")))


class Chain:
    def __init__(self, cfg, out, stable):
        self.cfg, self.R, self.name = cfg, hw(out), os.path.basename(hw(out).rstrip("/"))
        self.logs = self.R + ".logs"
        self.base = {"P2_TERV": cfg["p2_terv"], **({"P2_ORDER_STABLE": "1"} if stable else {})}

    def assemble(self):
        if os.path.exists(self.R):
            raise SystemExit(f"{self.R} exists")
        os.makedirs(self.logs, exist_ok=True)
        shared = os.path.join(P2, "assemble_fdr.json")
        keep = open(shared, "rb").read() if os.path.exists(shared) else None
        try:
            run([PY, "-I", "work/p2net/assemble_p2.py", "fdr"], f"{self.logs}/assemble_0.log",
                {**self.base, "P2_TAKE_ALL": "1", "P2_TAKE_P2_TILES": "1", "P2_OUT": self.R,
                 "P2_BUILD": f"{self.cfg['input_build']}/all"})
        finally:
            if keep is not None:
                open(shared, "wb").write(keep)
        run([PY, "-I", "work/p2net/add_lane_attrs.py", self.R, "work/p2net/w_fdr/profile_all.json"],
            f"{self.logs}/assemble_1.log", self.base)
        for f in stat_files(self.name):   # the adapter's rm -f floor_fix_<round>.json, for this test folder only
            if os.path.basename(f) == f"floor_fix_{self.name}.json":
                os.remove(f)

    def pass_(self, i, s, R=None, extra=None):
        R = R or self.R
        run([PY, "-I", f"work/p2net/{s}.py", R], f"{self.logs}/pass_{i:02d}_{s}{'' if R == self.R else '.twin'}.log",
            {**self.base, **self.cfg["pass_env"], **(extra or {})})

    def tail(self):
        env = {**self.base, "KEEP_UP": f"work/p2net/lightwell_{self.name}.json",
               "DOWN": self.cfg["pass_env"]["COVERED"], "UP": self.cfg["part_move_up"]}
        run([PY, "-I", "work/p2net/part_move.py", self.R], f"{self.logs}/part_move.log", env)
        for i, s in enumerate(self.cfg.get("after_part_move", [])):
            run([PY, "-I", f"work/p2net/{s}.py", self.R], f"{self.logs}/after_{i:02d}_{s}.log",
                {**self.base, **self.cfg["pass_env"]})
        for t in CBE:
            for f in (f"tile_{t}_above.glb", f"lanes_{t}.json"):
                shutil.copy2(os.path.join(HW, "work", "hw5", "b9", "all", f), os.path.join(self.R, f))


def twin(chain, i, s, seeds):
    """Run pass s, then again from the same input once per seed with the shuffle hook; one comparison per seed."""
    src = chain.R + ".in"
    if os.path.exists(src):
        raise SystemExit(f"{src} exists")
    shutil.copytree(chain.R, src, copy_function=shutil.copy2)
    out = []
    try:
        chain.pass_(i, s)
        for seed in seeds:
            t = f"{chain.R}.seed{seed}"
            if os.path.exists(t):
                raise SystemExit(f"{t} exists")
            before = stat_files(os.path.basename(t))
            try:
                shutil.copytree(src, t, copy_function=shutil.copy2)
                chain.pass_(i, s, R=t, extra={HOOK: str(seed)})
                out.append((seed, compare(chain.R, t)))
            finally:
                if os.path.isdir(t) and t.startswith(chain.R + "."):   # this run's own twin folder only
                    shutil.rmtree(t)
                for f in stat_files(os.path.basename(t)) - before:
                    os.remove(f)
    finally:
        if os.path.isdir(src) and src.startswith(chain.R + "."):
            shutil.rmtree(src)
    return out


def main(argv):
    if len(argv) >= 3 and argv[0] == "compare":
        r = compare(hw(argv[1]), hw(argv[2]))
        print(json.dumps({**r, "differ": r["differ"][:50]}, indent=1))
        return 0 if r["same"] else 1
    if len(argv) < 3 or argv[0] not in ("replay", "guard"):
        print(__doc__)
        return 2
    opt = dict(zip(argv[3::2], argv[4::2]))
    cfg = json.load(open(argv[1]))
    if argv[0] == "replay":
        c = Chain(cfg, argv[2], stable=opt.get("--stable", "0") == "1")
        c.assemble()
        for i, s in enumerate(cfg["passes"]):
            c.pass_(i, s)
        c.tail()
        print(json.dumps({"replayed": c.R, "passes": len(cfg["passes"]), "stable": opt.get("--stable", "0")}))
        return 0
    seeds = [int(x) for x in opt.get("--seeds", "1").split(",")]
    c = Chain(cfg, argv[2], stable=True)
    c.assemble()
    rep = {"round": cfg.get("round"), "seeds": seeds, "passes": []}
    out = c.R + ".guard.json"
    ok = True
    for i, s in enumerate(cfg["passes"]):
        if not hooked(s):
            c.pass_(i, s)
            rep["passes"].append({"i": i, "pass": s, "hooked": False})
            continue
        for seed, r in twin(c, i, s, seeds):
            rep["passes"].append({"i": i, "pass": s, "hooked": True, "seed": seed, "files": r["files"],
                                  "same": r["same"], "differ": r["differ"][:50], "only_a": r["only_a"][:20],
                                  "only_b": r["only_b"][:20]})
            print(f"{i:02d} {s}: seed {seed}: {'same' if r['same'] else 'DIFFERS: ' + str(len(r['differ'])) + ' files'}")
            ok = ok and r["same"]
        json.dump(rep, open(out, "w"), indent=1)
        if not ok:
            break
    rep["pass"] = ok
    json.dump(rep, open(out, "w"), indent=1)
    print(json.dumps({"guard": "pass" if ok else "FAIL", "report": out}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
