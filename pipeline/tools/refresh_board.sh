#!/bin/zsh
# Rebuild the Round Graph Board's data from the runs listed in $PIPELINE_RUNS/_board/runs.txt (one run folder per line).
# The upload to the board is done by the pipeline engineer session (Artifact asset upload + dataset url update).
set -e
RUNS=${PIPELINE_RUNS:?set PIPELINE_RUNS to the runs folder}
python3 "$(dirname $0)/board_data.py" "$RUNS" "$RUNS/_board/board_rows.json" ${(f)"$(cat $RUNS/_board/runs.txt)"}
