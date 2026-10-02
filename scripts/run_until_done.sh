#!/usr/bin/env bash
# Run the orchestrator in one uninterrupted execution until the project is
# finished or needs a human.
#
# `orchestrator run` dispatches only the tasks that are READY *now* (one
# wave), so "run everything" is a loop around it. `run --all` is that loop,
# implemented in orchestrator/cli.py where it can see task counts and health
# directly; this script is the same thing with argument checking and a
# project path default:
#
#   scripts/run_until_done.sh [project-dir] [max-concurrent]
#
# Exit codes (identical to `run --all`):
#   0   every task is terminal (DONE / CANCELLED)
#   3   blocked/stalled, a loop limit, or nothing left that can reach READY
#   4   a human decision is required
#   2   usage error (missing project, bad arguments)
#
# Examples:
#   scripts/run_until_done.sh /media/alireza/microos/projects/sys_mon_full 3
#   scripts/run_until_done.sh projects/kid-robot-face
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCH="$ROOT/bin/orchestrator"

PROJECT_INPUT="${1:-/media/alireza/microos/projects/sys_mon_full}"
MAX_CONCURRENT="${2:-3}"

if [ ! -x "$ORCH" ]; then
    echo "ERROR: orchestrator entry point not found: $ORCH" >&2
    exit 2
fi

# Relative project paths resolve from the caller's directory.
case "$PROJECT_INPUT" in
    /*) PROJECT="$PROJECT_INPUT" ;;
    *)
        PROJECT="$(cd "$PROJECT_INPUT" 2>/dev/null && pwd)"
        if [ -z "${PROJECT:-}" ]; then
            echo "ERROR: no such project directory: $PROJECT_INPUT" >&2
            exit 2
        fi
        ;;
esac

if [ ! -f "$PROJECT/PROJECT.yaml" ] || [ ! -f "$PROJECT/TASKS.yaml" ]; then
    echo "ERROR: not a project (missing PROJECT.yaml/TASKS.yaml): $PROJECT" >&2
    exit 2
fi

echo "project:        $PROJECT"
echo "max-concurrent: $MAX_CONCURRENT"
echo

exec "$ORCH" --project "$PROJECT" run --all --max-concurrent "$MAX_CONCURRENT"
