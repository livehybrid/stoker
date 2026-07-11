#!/usr/bin/env bash
# Standalone smoke test: run the Stoker worker against tools/hec_sink.py and
# assert the received event count. Used by CI and `make docker-smoke`.
#
# Usage:
#   tools/smoke.sh local            run via the checkout (needs deps installed)
#   tools/smoke.sh docker <image>   run the built image with host networking
#
# Env overrides:
#   SINK_PORT      sink port (default 18088)
#   HEC_TOKEN      token the sink requires (default smoke-token)
#   RATE           STOKER_RATE_VALUE in EPS (default 100)
#   DURATION_S     STOKER_DURATION_S (default 20)
#   TOLERANCE_PCT  assert events within +/- this % of RATE*DURATION_S;
#                  empty means only assert events > 0 and exit code 0
#   PACK           bundle directory (default packs/flatline)
#   PYTHON         python interpreter (default python3)
set -euo pipefail

MODE="${1:?usage: smoke.sh local|docker [image]}"
IMAGE="${2:-}"
if [ "$MODE" = "docker" ] && [ -z "$IMAGE" ]; then
    echo "smoke.sh: docker mode needs an image reference" >&2
    exit 2
fi
if [ "$MODE" != "local" ] && [ "$MODE" != "docker" ]; then
    echo "smoke.sh: unknown mode '$MODE' (expected local or docker)" >&2
    exit 2
fi

PYTHON="${PYTHON:-python3}"
SINK_PORT="${SINK_PORT:-18088}"
HEC_TOKEN="${HEC_TOKEN:-smoke-token}"
RATE="${RATE:-100}"
DURATION_S="${DURATION_S:-20}"
TOLERANCE_PCT="${TOLERANCE_PCT:-}"
PACK="${PACK:-packs/flatline}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The agent stamps the bundle's samples path into the rewritten conf and
# eventgen resolves relative sampleDir against the conf's own directory
# (the run workdir), so the bundle path must be absolute.
case "$PACK" in
    /*) ABS_PACK="$PACK" ;;
    *) ABS_PACK="$REPO_ROOT/$PACK" ;;
esac

"$PYTHON" tools/hec_sink.py --port "$SINK_PORT" --token "$HEC_TOKEN" &
SINK_PID=$!
cleanup() { kill "$SINK_PID" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 50); do
    if curl -fsS "http://127.0.0.1:${SINK_PORT}/stats" >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
curl -fsS "http://127.0.0.1:${SINK_PORT}/stats" >/dev/null # loud failure if the sink never came up

# Hard ceiling so a hung agent fails the job instead of wedging it.
HARD_TIMEOUT=$((DURATION_S + 60))
TIMEOUT_CMD=()
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD=(timeout "$HARD_TIMEOUT")
fi

AGENT_RC=0
if [ "$MODE" = "local" ]; then
    ${TIMEOUT_CMD[@]+"${TIMEOUT_CMD[@]}"} env \
        STOKER_STANDALONE=1 \
        STOKER_BUNDLE="$ABS_PACK" \
        STOKER_HEC_URL="http://127.0.0.1:${SINK_PORT}" \
        STOKER_HEC_TOKEN="$HEC_TOKEN" \
        STOKER_INDEX=main \
        STOKER_RATE_MODE=eps \
        STOKER_RATE_VALUE="$RATE" \
        STOKER_DURATION_S="$DURATION_S" \
        STOKER_METRICS_PORT=0 \
        PYTHONPATH="worker:worker/engines/eventgen" \
        "$PYTHON" -m stoker_agent || AGENT_RC=$?
else
    ${TIMEOUT_CMD[@]+"${TIMEOUT_CMD[@]}"} docker run --rm --network host \
        -v "${ABS_PACK}:/bundle:ro" \
        -e STOKER_STANDALONE=1 \
        -e STOKER_BUNDLE=/bundle \
        -e STOKER_HEC_URL="http://127.0.0.1:${SINK_PORT}" \
        -e STOKER_HEC_TOKEN="$HEC_TOKEN" \
        -e STOKER_INDEX=main \
        -e STOKER_RATE_MODE=eps \
        -e STOKER_RATE_VALUE="$RATE" \
        -e STOKER_DURATION_S="$DURATION_S" \
        -e STOKER_METRICS_PORT=0 \
        "$IMAGE" || AGENT_RC=$?
fi

STATS="$(curl -fsS "http://127.0.0.1:${SINK_PORT}/stats")"
kill -TERM "$SINK_PID" 2>/dev/null || true
wait "$SINK_PID" 2>/dev/null || true
trap - EXIT

echo "smoke: agent rc=${AGENT_RC} stats=${STATS}"

STATS="$STATS" AGENT_RC="$AGENT_RC" RATE="$RATE" DURATION_S="$DURATION_S" \
    TOLERANCE_PCT="$TOLERANCE_PCT" "$PYTHON" - <<'PY'
import json
import os
import sys

stats = json.loads(os.environ["STATS"])
events = int(stats.get("events", 0))
rc = int(os.environ["AGENT_RC"])
rate = float(os.environ["RATE"])
duration = float(os.environ["DURATION_S"])
tolerance = os.environ.get("TOLERANCE_PCT") or ""

failures = []
if rc != 0:
    failures.append("agent exited %d, expected 0" % rc)
if tolerance:
    expected = rate * duration
    lo = expected * (1 - float(tolerance) / 100.0)
    hi = expected * (1 + float(tolerance) / 100.0)
    if not lo <= events <= hi:
        failures.append(
            "events=%d outside [%.0f, %.0f] (expected %.0f +/- %s%%)"
            % (events, lo, hi, expected, tolerance)
        )
elif events <= 0:
    failures.append("no events received")

if failures:
    print("SMOKE FAIL: " + "; ".join(failures))
    sys.exit(1)
print("SMOKE OK: events=%d agent rc=0" % events)
PY
