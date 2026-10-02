#!/usr/bin/env bash
# Run the orchestrator in one uninterrupted execution until the project is
# finished or needs a human.
#
# `orchestrator run` has no --all flag: each invocation dispatches the tasks
# that are READY *now* (default --max-tasks 25). This loop is the mechanism
# that keeps going until every task is DONE — or until it is honest to stop:
#
#   * every task terminal (DONE / CANCELLED)   -> SUCCESS, exit 0
#   * BLOCKED / STALLED / HUMAN_DECISION_REQUIRED health -> stop, exit 3
#   * `run` exits non-zero (loop limit, stalled, human decision) -> stop
#   * nothing READY and not yet finished       -> stop (never spin)
#
# Usage:
#   scripts/run_until_done.sh [project-dir] [max-concurrent]
#
# Examples:
#   scripts/run_until_done.sh /media/alireza/microos/projects/sys_mon_full 3
#   scripts/run_until_done.sh projects/kid-robot-face
#
# No approval prompts: it dispatches whatever the graph says is READY.
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

iteration=0
while true; do
    iteration=$((iteration + 1))

    STATUS="$("$ORCH" --project "$PROJECT" status 2>&1)"
    STATUS_CODE=$?
    if [ "$STATUS_CODE" -ne 0 ]; then
        printf '%s\n' "$STATUS"
        echo "STOPPED: 'status' exited $STATUS_CODE." >&2
        exit "$STATUS_CODE"
    fi

    # Finished = the TASK SUMMARY holds no non-terminal status. Deciding on the
    # counts rather than on `Phase: RELEASE` alone matters because
    # phase.current is stored and forward-only: a reopened or re-planned task
    # makes it stale, and claiming SUCCESS on a stale phase would be a lie.
    summary="$(printf '%s\n' "$STATUS" | sed -n '/^TASK SUMMARY:/,/^$/p')"
    total="$(awk '/^  TOTAL/ {print $2}' <<<"$summary")"
    active="$(grep -E '^  [A-Z]' <<<"$summary" \
        | grep -EvE '^  (DONE( WITH ACCEPTED LIMITATION)?|CANCELLED|TOTAL) ' || true)"

    if [ "${total:-0}" -gt 0 ] && [ -z "$active" ]; then
        echo "============================================================"
        echo "SUCCESS: every task is terminal (DONE / CANCELLED)."
        echo "============================================================"
        printf '%s\n' "$STATUS" | grep -E '^(Phase|Status|Health):' || true
        printf '%s\n' "$summary" | grep ' tasks$' || true
        exit 0
    fi

    if grep -Eq '^Health: *(BLOCKED|STALLED|HUMAN_DECISION_REQUIRED)' <<<"$STATUS"; then
        echo "============================================================"
        echo "STOPPED: the pipeline needs attention or a decision:"
        printf '%s\n' "$STATUS" | grep -E '^(Phase|Status|Health):|^  - ' || true
        echo "============================================================"
        echo "inspect: $ORCH --project $PROJECT health --diagnose"
        exit 3
    fi

    echo "--- iteration $iteration: dispatching READY tasks ---"
    RUN_OUT="$("$ORCH" --project "$PROJECT" run --max-concurrent "$MAX_CONCURRENT" 2>&1)"
    RUN_CODE=$?
    printf '%s\n' "$RUN_OUT"

    # 3 = loop limit / stalled / blocked, 4 = human decision required,
    # 2 = usage or state error. All of them mean "stop, do not re-dispatch".
    if [ "$RUN_CODE" -ne 0 ]; then
        echo "============================================================"
        echo "STOPPED: 'run' exited $RUN_CODE (loop limit, blocked/stalled,"
        echo "human decision, or a state error) — see the output above."
        echo "============================================================"
        echo "next:   $ORCH --project $PROJECT status"
        exit "$RUN_CODE"
    fi

    # Idle guard: healthy, but nothing can be dispatched. Without this the loop
    # would re-run forever on a graph whose remaining tasks have no path to READY.
    if grep -q 'No READY tasks available' <<<"$RUN_OUT"; then
        echo "============================================================"
        echo "STOPPED: no READY tasks left and the project is not finished."
        echo "============================================================"
        printf '%s\n' "$STATUS" | grep -E '^(Phase|Status|Health):|READY |BLOCKED |TODO ' || true
        echo "inspect: $ORCH --project $PROJECT status"
        exit 3
    fi

    sleep 1
done
