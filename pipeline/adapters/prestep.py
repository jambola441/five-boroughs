"""A round's optional pre-step for agy swarm steps (swarm_ext.py, swarm_plan.py): a "prestep" block names a module
outside this repo (a project path under NYC_ROOT) that prepares each step's working folder before its agent starts:

  "prestep": {"module": "work/<folder>/<module>.py", ...its own options}

The module provides wrap(S, cfg, item, part) (called before the step's agent) and done(item) (after the item's check).
Nothing project-specific lives here; the module and its data stay in the private project tree.
"""
import importlib.util
import os

from common import project

_MOD = {}


def _load(cfg):
    p = (cfg.get("prestep") or {}).get("module")
    if not p:
        return None
    path = project(p)
    if path not in _MOD:
        spec = importlib.util.spec_from_file_location("round_prestep", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MOD[path] = m
    return _MOD[path]


def wrap(S, cfg, item, part):
    m = _load(cfg)
    if m:
        m.wrap(S, cfg, item, part)


def done(cfg, item):
    m = _load(cfg)
    if m:
        m.done(item)
