# attack-replay — a Stoker raw-replay (Piston) pack

This is an example pack for **Piston**, Stoker's raw-replay worker engine. Where
the eventgen packs (`packs/flatline`, `packs/apigw`) *template* events from a
sample, Piston **replays a recorded dataset byte-for-byte**: the exact events,
re-timestamped to now, at a chosen rate or at the recorded cadence.

The bundled dataset (`dataset/events.log`) is a small Sysmon + Windows-Security
capture in the shape of a
[splunk/security_content](https://github.com/splunk/security_content) `attack_data`
slice: a credential-access and lateral-movement chain (encoded PowerShell,
`comsvcs.dll MiniDump` of LSASS, SAM/SYSTEM hive dump, domain discovery, WMI
remote exec, a service and scheduled-task for persistence, shadow-copy deletion,
and clean-up). These are the events Piston replays.

## rawreplay vs eventgen

| | eventgen (`flatline`, `apigw`) | rawreplay / Piston (`attack-replay`) |
|---|---|---|
| Events | Templated from a sample; tokens randomised each pass | The recorded dataset, **byte-for-byte** |
| Timestamps | Engine stamps from `token`/`earliest`/`latest` | Re-stamped to now (rate mode) or recorded gaps × `time_multiple` (cadence mode) |
| `pack.yaml` key | `engine: eventgen` | `engine: rawreplay` |
| Where the payload lives | `samples/*.sample` | `dataset/<file>` (declared in `replay.dataset`) |
| Workers | fan-out across N workers | **single worker** (the control plane enforces `replay_single_worker`) |
| Conf rewrite | agent rewrites `default/eventgen.conf` for its share | agent **skips** the rewrite and launches Piston |

In both cases the worker **agent** owns metadata (index/sourcetype/host/source
from the run slice), HEC delivery and pacing. The engine only produces events
over the unix socket; it never speaks to HEC or the control plane.

## How Piston paces (matches the worker contract)

- **rate mode** (`replay.mode: rate`; run `rate_mode` = `eps` or `per_day_gb`):
  Piston emits events HOT with `time = null`, the agent stamps *now* and its
  token bucket delivers at the exact rate. The dataset **loops** to fill the run
  duration. This is what this pack ships (`mode: rate`).
- **cadence mode** (`replay.mode: cadence`; run `rate_mode` = `count_interval`):
  Piston self-paces, reproducing the recorded inter-event gaps × `time_multiple`
  and setting `time = now + offset`. No token-bucket gating (engine-paced), which
  is why replay is pinned to one worker.

## pack.yaml `replay:` block

```yaml
engine: rawreplay
replay:
  dataset: dataset/events.log   # path to the recorded capture, relative to the pack root
  mode: rate                    # rate | cadence
  time_multiple: 1.0            # cadence-mode gap multiplier (1.0 = real time; <1 faster, >1 slower)
  dataset_url: https://github.com/splunk/security_content/tree/develop/datasets/attack_techniques
estimates:
  bytes_per_event: 1591         # measured mean of dataset/events.log; drives per_day_gb gating
  per_day_gb: 0.05
defaults:
  index: main
  sourcetype: XmlWinEventLog    # a realistic Sysmon/WinEventLog sourcetype
```

Here `dataset_url` is **provenance only**: because a local `replay.dataset` is
present, the control plane keeps the on-disk file and never fetches the URL (it
just records where the capture came from). A `dataset_url` becomes an *actionable*
https fetch source only when a pack ships **no** local `dataset` — then the
control plane downloads it at build time (https-only, public-host, size-capped,
sha-pinned) and embeds the bytes in the bundle. Piston always reads events from
the dataset inside the bundle, never from the network.

For the full pack-format reference (both engines, every field, the git-sync and
`dataset_url` safety rules) see [`docs/PACKS.md`](../../docs/PACKS.md).

## Pointing this pack at a real attack_data dataset

`splunk/security_content` ships hundreds of real captures under
`datasets/attack_techniques/<technique>/…` (mostly `*.log` / `XmlWinEventLog`,
some `*_json.txt`). To replay one for real:

1. Fetch the raw capture into this pack, e.g.

   ```bash
   curl -fsSL \
     https://media.githubusercontent.com/media/splunk/attack_data/master/datasets/attack_techniques/T1003.001/atomic_red_team/windows-sysmon.log \
     -o dataset/events.log
   ```

   (`splunk/attack_data` stores the large captures via Git LFS, so use the
   `media.githubusercontent.com` raw URL or `git lfs pull`, not the HTML page.)

2. Set `replay.dataset` to that file and `defaults.sourcetype` to match the
   capture (`XmlWinEventLog` for Sysmon/WinEventLog, `linux_secure` for
   `/var/log/secure`, etc.). Update `replay.dataset_url` to the source for the
   audit trail.

3. Re-measure `estimates.bytes_per_event` (mean line length) so `per_day_gb`
   pacing stays accurate — the control plane also measures this at lint time
   when it is omitted.

4. Register + run:

   ```bash
   # register the local pack (control plane lints it)
   curl -sX POST "$STOKER/api/packs" \
     -H 'content-type: application/json' \
     -d '{"name":"attack-replay","source_path":"/abs/path/to/packs/attack-replay"}'
   # create a spec with engine=rawreplay, workers=1, then POST /api/specs/{id}/run
   ```

   Replay runs are single-worker by contract: a spec with `workers > 1` on a
   rawreplay pack is rejected `409 replay_single_worker`.

## Files

```
packs/attack-replay/
  pack.yaml               name/engine/replay/estimates/defaults (author metadata)
  default/eventgen.conf   one mode=replay stanza (lint + eventgen-fallback; skipped by Piston)
  dataset/events.log      the recorded capture Piston replays byte-for-byte (34 events)
  README.md               this file
```
