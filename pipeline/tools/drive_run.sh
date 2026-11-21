#!/bin/zsh
# Keep a run moving while its long steps run as detached jobs (Irrigator ends any command step at 120 s and never
# replays a command step by itself, so each one is retried explicitly here).
#   tools/drive_run.sh RUN_DIR [seconds between checks, default 60]
# Each pass: `irrigator retry` every step whose error says "job running", then `irrigator resume`. It stops when no
# step is running a job: the run is done, or it waits on people (frames, lead approval) or on a real failure.
# With WAITING=1 it also retries the steps that wait on people ("awaiting ..."), e.g. after frames or approval.json
# were dropped in. Safe to stop and start again at any time. Needs NYC_ROOT in the environment.
RUN=${1:?run dir}; GAP=${2:-60}; OUT=${RUN:h}
export PIPELINE_OUT=$OUT
pick() {  # step ids whose last error matches $1
  /usr/bin/python3 -I -c '
import json, re, sys
s = json.load(open(sys.argv[1] + "/state.json"))
print(" ".join(k for k, t in s["tasks"].items() if t.get("status") == "failed" and re.search(sys.argv[2], t.get("error") or "")))' "$RUN" "$1"
}
first=1
while true; do
  ids=$(pick "job running|GB free; need")
  [[ -n $WAITING && -n $first ]] && ids="$ids $(pick '^awaiting')"
  first=
  for id in ${=ids}; do irrigator retry "$RUN" "$id" >> "$OUT/drive.log" 2>&1; done
  irrigator resume "$RUN" > "$OUT/drive_last.txt" 2>&1
  left=$(pick "job running|GB free; need")
  date "+%H:%M:%S running: ${left:-none}" >> "$OUT/drive.log"
  [[ -z $left ]] && break
  sleep $GAP
done
irrigator status "$RUN" >> "$OUT/drive.log" 2>&1
