# Stoker integration harness

A pytest suite that drives a **live** Stoker deployment over its operator API and
asserts what actually lands in Splunk. It is separate from the unit suites
(`server/tests`, `worker/tests`, `tools/tests`) and is never run by the normal CI.

Everything is kept small on purpose (a low eps and a short window) so storage and
search time stay negligible.

## What it checks

| File | Needs | Checks |
|------|-------|--------|
| `test_api.py` | `STOKER_URL` + `STOKER_TOKEN` (target/estimate tests also need a HEC target) | auth is enforced, target + metric-pack CRUD, and the backfill **estimate** (incl. the "honour the eps" rate behaviour) |
| `test_end_to_end.py` | a HEC target + an index (+ Splunk for the count) | launches a tiny **metrics backfill** and a tiny **eventgen** run, waits for `completed`, asserts the delivered total and (with Splunk) the indexed count |

The suite degrades gracefully: no `STOKER_URL`/`STOKER_TOKEN` skips everything; no
HEC target skips the live-run tests; no Splunk config asserts the run's own
delivered total instead of the indexed count.

## 1. Mint an operator token

Token management is admin-only and there is no UI page for it, so mint over the
API with your own login (the stack `.env` admin password may have drifted, so use
the credentials you log into the UI with):

```bash
export STOKER=https://stoker.mydomain.com

# log in -> session cookie
curl -sk -c /tmp/stoker.cookies -X POST $STOKER/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<you>","password":"<your-password>"}'

# mint an operator token (the "token" field is shown ONCE)
curl -sk -b /tmp/stoker.cookies -X POST $STOKER/api/tokens \
  -H 'Content-Type: application/json' \
  -d '{"name":"integration-harness","role":"operator","expires_in_days":90}'
```

Copy the `stk_...` value into `STOKER_TOKEN`. `operator` is enough (it can create
targets/specs and launch runs); it cannot manage users or other tokens.

## 2. Configure

```bash
cp .env.example .env      # fill in the values
set -a; . ./.env; set +a  # export them into the shell
```

Roles for the indexes: `STOKER_TEST_INDEX` must be an **events** index and
`STOKER_TEST_METRIC_INDEX` a **metrics-type** index (metric points are searched
with `mstats`, not `search`). Point them at scratch/lab indexes you can purge.

## 3. Run

```bash
pip install -r requirements.txt
pytest harness                 # from the repo root
pytest harness -k estimate     # just the estimate/contract tests (no launching)
pytest harness -k end_to_end   # just the live runs
```

## How it stays isolated + cheap

- Each run tags its data with a unique `source` override, so the Splunk count is
  filtered to exactly that run (`index=... source="stoker-harness-..."`), immune
  to other data in the index.
- Targets and specs are created per test and deleted on teardown. The metric pack
  has no delete endpoint, so it is created once (session scoped) and reused by
  name (`stoker-harness-metric`).
- Volumes: the eventgen run is `STOKER_TEST_EPS` (default 5) for
  `STOKER_TEST_DURATION_S` (default 15 s) ≈ 75 events; the metrics backfill is
  `window / resolution × series` (default 300/30 × 2 ≈ 20 points). Re-running a
  backfill appends duplicate points, so the metrics count assertion uses `>=`.

## CI

Provide `STOKER_URL` + `STOKER_TOKEN` (and optionally the HEC/Splunk secrets) as
CI secrets and run `pytest harness`. With only the first two it runs as an
API-contract smoke; add the HEC target + Splunk to make it a full end-to-end gate.
