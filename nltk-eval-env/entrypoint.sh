#!/bin/sh
# Entry point for a single evaluation run. Expects /task/task.json to
# describe the patch under test (mounted read-only) and writes its
# verdict to /results/result.json.
set -eu

TASK_DIR="${TASK_DIR:-/task}"
RESULTS_DIR="${RESULTS_DIR:-/results}"

mkdir -p "$RESULTS_DIR"

exec python /opt/run_eval.py \
    --repo /repo \
    --task "$TASK_DIR/task.json" \
    --out "$RESULTS_DIR/result.json"
