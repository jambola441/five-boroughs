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
