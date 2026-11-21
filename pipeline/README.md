# Round graphs (pilot)

Each terrain round runs as one [Irrigator](https://github.com/grainulation/irrigator) graph. The terrain
coordinator, the QA coordinator and the lead are nodes in it, so QA can't start before the build is accepted,
and only the lead gate can write to the register.

```
pin_inputs -> terrain_checks -> terrain_accept (Claude)
  -> disk_guard -> drive_gate, city_census, hydrogen_<pair> (in parallel)
  -> qa_accept (Claude + gate rule in code) -> lead_gate (approval file) -> register_update
```

- `rounds/*.json`: one file per round: frozen inputs (paths relative to `NYC_ROOT`), pins, frame pairs.
- `build_round.py`: turns a round file into `plan.json` plus a new run folder.
- `adapters/`: command adapters that speak `swarm.worker/v1`. They only read the project and write only
  inside the run folder.
- `prompts/`: what the Claude coordinator nodes are asked.

## Run

```sh
export NYC_ROOT=/path/to/nyc-buildings
export PIPELINE_OUT=/path/outside/the/repo/runs/r6r-shadow-1   # must not exist yet
python3 pipeline/build_round.py pipeline/rounds/r6r_shadow.json "$PIPELINE_OUT"
irrigator apply "$PIPELINE_OUT/plan.json" --run "$PIPELINE_OUT/run" --backend mock --workers 3
irrigator status "$PIPELINE_OUT/run"
# after reading the QA verdict, as lead:
echo '{"decision":"approve","by":"lead"}' > "$PIPELINE_OUT/approval.json"
irrigator retry "$PIPELINE_OUT/run" lead_gate && irrigator resume "$PIPELINE_OUT/run"
```

Needs `irrigator`, the `claude` CLI, and `agy`, all signed in on this machine. No keys, page source or
data go in this repo. It's public.

## Long steps run as jobs

Irrigator ends any command step (and any validator) after 120 s; a swarm/v1 plan can't raise that. It also never
replays a command step by itself. So every team script that may run longer is started as a detached job
(`common.run_job`): the first call launches it in its own session and returns `not_started` "job running"; a later
call settles it once it has exited. Each job runs at most once per run folder (`<run>/jobs/<step>/`).

```
NYC_ROOT=... tools/drive_run.sh <run>/run          # retries "job running" steps, resumes, until none is left
WAITING=1 NYC_ROOT=... tools/drive_run.sh <run>/run  # also retries steps awaiting people (frames, approval.json)
```

## Page rounds (kind `page_round`)

These wrap the page editor's tools, unchanged, as its round-graph spec (kept local) lists them:
drape, lift_gate, newlist, deck_under, ramps, trace, lidar and census.
- Steps are listed in the round file.
- Gates are computed in code against the round's declared budget; 0 means "must not rise".
- Frames are a hand-off. `frames_ready` writes `frames_request.json` (poses, variants and, for trace rounds,
  sites per class) and waits until the page-editor session has rendered into the named folder and dropped an empty
  `READY` file. Hydrogen then reviews each image in a copy-only folder, with commands denied.
- The page editor's folder is reached only through `PE_ROOT` (`pe:` paths in round files). Its code is called by
  local path only. It is never copied, committed or passed to agy. The commit scanner is an allowlist (pipeline
  code, prompts, round files, docs) plus the sha256 of every file in the page editor's code trees.
- Rounds:
  - `page_trace_rl1.json`: round 1, the trace;
  - `page_bpp_r2.json`: round 2, B″ citywide, with a DRAFT budget for the lead to set;
  - `page_wiring_r6s2_od.json`: round 2's graph on existing outputs.
