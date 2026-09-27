#!/bin/sh
# Nightly integration run against a live Stoker (see README "Nightly").
#
#   harness/nightly.sh <env file> [<python>]
#
# Sources the env file (STOKER_URL, STOKER_TOKEN, STOKER_TEST_*, SPLUNK_*),
# runs the harness, keeps a log under $STOKER_HARNESS_LOG_DIR (default
# ~/stoker-harness-logs, last 30 kept) and, when STOKER_TEST_HEC_URL/TOKEN are
# set, posts one summary event (sourcetype stoker:harness) so a failed night is
# searchable and alertable in Splunk. Exit status is pytest's.
set -u
ENV_FILE=${1:?usage: nightly.sh <env file> [python]}
PY=${2:-python3}
HERE=$(cd "$(dirname "$0")" && pwd)
LOG_DIR=${STOKER_HARNESS_LOG_DIR:-$HOME/stoker-harness-logs}
mkdir -p "$LOG_DIR"
set -a
. "$ENV_FILE"
set +a
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG="$LOG_DIR/harness-$STAMP.log"
START=$(date +%s)
cd "$HERE/.." && "$PY" -m pytest harness -p no:cacheprovider -rA > "$LOG" 2>&1
RC=$?
DUR=$(( $(date +%s) - START ))
count() { grep -c "^$1" "$LOG"; }
SUMMARY="$(count PASSED) passed, $(count FAILED) failed, $(count ERROR) errors, $(count SKIPPED) skipped"
ls -1t "$LOG_DIR"/harness-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f
if [ -n "${STOKER_TEST_HEC_URL:-}" ] && [ -n "${STOKER_TEST_HEC_TOKEN:-}" ]; then
  "$PY" - "$RC" "$DUR" "$SUMMARY" "$STAMP" <<'EOF'
import json, os, sys, urllib.request
rc, dur, summary, stamp = sys.argv[1:5]
event = {"harness": "stoker", "status": "pass" if rc == "0" else "fail", "exit_code": int(rc),
         "duration_s": int(dur), "summary": summary, "run": stamp, "stoker_url": os.environ.get("STOKER_URL")}
body = json.dumps({"event": event, "sourcetype": "stoker:harness", "source": "stoker-harness-nightly",
                   "index": os.environ.get("STOKER_TEST_INDEX", "main")}).encode()
req = urllib.request.Request(os.environ["STOKER_TEST_HEC_URL"].rstrip("/") + "/services/collector/event",
                             data=body, headers={"Authorization": "Splunk " + os.environ["STOKER_TEST_HEC_TOKEN"]})
try:
    urllib.request.urlopen(req, timeout=15).read()
except Exception as exc:  # the summary is best effort; the log file is the record
    print("summary not sent: %s" % exc, file=sys.stderr)
EOF
fi
exit $RC
