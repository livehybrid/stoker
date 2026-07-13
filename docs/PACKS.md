# Stoker pack format

A **pack** is the unit Stoker turns into a load-generation workload. It carries
the event material (an eventgen template + samples, or a recorded dataset), the
metadata the worker stamps onto events, and a volume estimate. The control plane
lints a pack, builds a content-addressed bundle from it, and the worker unpacks
that bundle and streams events to HEC.

This file is the authoritative pack-format reference. It is verified against:

- `server/bundles.py` — `lint_pack` / `lint_rawreplay_pack` (the linter) and
  `parse_replay_config` (the `replay:` validator).
- `server/gitsync/sync.py` — pack discovery, the custom-code guards, the
  `verified` flag and path-escape rejection.
- `worker/stoker_agent/bundle.py` — how the worker reads a bundle at run time.

The buildable worker spec is [`WORKER-CONTRACT.md`](WORKER-CONTRACT.md); this file
expands the "pack / bundle shape" it references.

There are **two pack kinds**, selected by the engine:

| | eventgen (default) | rawreplay (Piston) |
|---|---|---|
| Purpose | *Template* events from a sample, tokens re-randomised each pass | *Replay* a recorded dataset **byte-for-byte**, re-timestamped to now |
| Required file | `default/eventgen.conf` | `pack.yaml` with `engine: rawreplay` + a `replay:` section |
| Payload | `samples/*` | a dataset file (`replay.dataset`) or an https `replay.dataset_url` |
| Examples | `packs/flatline`, `packs/apigw`, `packs/web-access`, `packs/aws-cloudtrail`, `packs/aws-s3-access`, `packs/aws-elb-alb` | `packs/attack-replay` |
| Workers | fan-out across N | **1** (control plane forces it; `409 replay_single_worker`) |

Both kinds share the same `pack.yaml` metadata block (`name`, `description`,
`engine`, `estimates`, `defaults`). Output-side metadata (index, sourcetype,
host, source) is **stamped by the worker agent from the run slice** — never set
in `default/eventgen.conf`. The `defaults:` block in `pack.yaml` supplies the
pack's suggested index/sourcetype that the control plane pre-fills at submit.

---

## `pack.yaml`

`pack.yaml` is parsed by a deliberately small **flat two-level scalar subset**
parser (`stoker_agent.bundle.parse_pack_yaml`, mirrored in
`server/bundles._parse_pack_yaml` so the control plane needs no worker import).
It is **not** a YAML parser. The rules:

- Top-level `key: value` scalars, and **one** indented level of `key: value`
  under a bare `key:` line. Nothing deeper.
- **Every value must be on a single line.** Multi-line and block scalars are not
  supported.
- **List values are dropped** (a `- item` line is skipped with a warning). This
  is why `tags:` in a synced pack is usually ignored unless written as a
  single-line comma string (`tags: web, access`), which git-sync splits on
  commas.
- Comments (`#` to end of line, outside quotes) and blank lines are ignored.
- Scalars coerce: `true/false/yes/no/on/off` → bool, `null/~/empty` → null,
  then int, then float, else string. Quote a value to force a string.

Recognised keys:

| Key | Level | Meaning |
|---|---|---|
| `name` | top | Pack name (the row key within a repo). Falls back to the directory basename, or the repo URL slug for a repo-root pack. |
| `description` | top | Free-text description surfaced in the UI/API. |
| `engine` | top | `eventgen` (default) or `rawreplay`. |
| `estimates.bytes_per_event` | nested | Mean event size in bytes; drives `per_day_gb` pacing. Measured from the first sample (eventgen) or the dataset (rawreplay) when omitted. |
| `estimates.per_day_gb` | nested | Optional declared daily volume the pack is shaped for. |
| `defaults.index` | nested | Suggested index (pre-filled at submit; the run slice's index wins). |
| `defaults.sourcetype` | nested | Suggested sourcetype (also read by the linter). |
| `defaults.source` | nested | Suggested source (rawreplay passes this through to the engine). |
| `replay.*` | nested | rawreplay only — see [below](#rawreplay-piston-packs). |

> `estimates.bytes_per_event` may also be given as top-level `declared_per_day_gb`
> for the per-day figure; the nested `estimates.per_day_gb` is preferred.

---

## eventgen packs

### Layout

```
packs/<name>/
  pack.yaml               # name / engine / estimates / defaults (metadata)
  default/eventgen.conf   # REQUIRED: one or more sample/replay stanzas
  samples/                # sample files referenced by the stanzas
    <name>.sample
    ...
```

`default/eventgen.conf` is the required file — its presence is what marks a
directory as an eventgen pack root. The engine runs with its working directory
rooted at the pack, so file-token paths (`token.N.replacement = samples/foo`)
resolve against the pack.

### What the linter checks (`lint_pack`)

- The conf parses (`RawConfigParser`, `delimiters=("=",)`, `optionxform = str`
  so keys stay case-sensitive).
- At least one non-`[global]`/`[default]` stanza exists.
- Every stanza's `mode` is `sample` or `replay` (default `sample`). Anything
  else is an error.
- A sample-mode stanza resolves a sample file: `sampleFile = <name>` (or the
  stanza name) must exist under `samples/` or at the pack root.
- Every `*.token` value is a compilable regex.
- Derives `sourcetypes` (from the stanzas and `defaults.sourcetype`), `stanzas`,
  `engines` and a `bytes_per_event` estimate (measured from the first sample
  when `pack.yaml` omits it).

Output-side keys in the conf (`index`, `sourcetype`, `host`, `source`,
`outputMode`) are tolerated but ignored — the worker strips them and stamps
metadata from the slice. **Do not rely on them; set metadata via `defaults:` and
the run slice.**

### Minimal working example — `packs/flatline`

`pack.yaml`:

```yaml
name: flatline
engine: eventgen
description: "Steady single-line web service log at a constant rate"
estimates:
  bytes_per_event: 120
defaults:
  index: main
  sourcetype: stoker:flatline
```

`default/eventgen.conf`:

```ini
[flatline.sample]
mode = sample
interval = 1
count = 100
earliest = -1s
latest = now

token.0.token = \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}
token.0.replacementType = timestamp
token.0.replacement = %Y-%m-%dT%H:%M:%S
```

`samples/flatline.sample` (one representative event per line):

```
2026-07-10T12:00:00.000Z host=web1 svc=checkout level=INFO msg="request completed" status=200 bytes=5312 dur_ms=48 trace=a1b2c3
```

The worker rewrites `interval`/`count` (or `perDayVolume`) for its share of the
rate; `hourOfDayRate` and the other `*Rate` shaping maps are preserved verbatim
in `per_day_gb`/`count_interval` modes (see `packs/apigw` for a diurnal example).

### Token replacement: capture groups and the `%s` epoch gotcha

Two behaviours of the vendored eventgen (7.2.1) matter when authoring tokens, and
both are honoured by the in-app [preview renderer](#) too:

- **Capture group replaces the group, not the whole match.** A `token.N.token`
  regex with a capturing group has **only group 1** substituted; the literal text
  on either side of it (inside the match) is preserved. This is what lets a JSON
  or delimited pack rewrite a value in place: `"sourceIPAddress":"(\d+\.\d+\.\d+\.\d+)"`
  keeps the key and quotes, `\] (\d+\.\d+\.\d+\.\d+)` keeps the `] ` before the
  address, and `srcip=(\d...)` keeps `srcip=`. A **groupless** regex replaces the
  whole match (use this for a bare timestamp like `\d{4}-\d{2}-\d{2}T...`).
- **Avoid `%s` epoch timestamps.** The vendored eventgen builds an epoch for a
  `%s` replacement as `str(epoch).rstrip("0")`, which strips trailing zeros — so
  any epoch ending in `0` is silently corrupted (e.g. `1625097600` → `16250976`).
  Do **not** use `%s` timestamp tokens. For a format that carries a Unix epoch
  (e.g. VPC Flow Logs), either leave the recorded epoch (the agent stamps the HEC
  envelope `time` to now regardless, so `_time` is correct) or template the event
  around a human-readable timestamp field instead.

### sample vs replay mode inside eventgen.conf

`mode` in `eventgen.conf` is an eventgen concept distinct from the pack engine:

- `mode = sample` — eventgen picks lines from the sample and re-applies tokens
  each pass (the normal templated pack).
- `mode = replay` — eventgen streams the sample in order at the recorded cadence
  (`timeMultiple`). This is eventgen's own replay and is a valid, lint-passing
  fallback. **It is not Piston.** A pack that wants true byte-for-byte Piston
  replay declares `engine: rawreplay` (next section); a rawreplay pack also ships
  a `mode = replay` conf purely to satisfy the eventgen-pack contract.

The worker never rewrites a `mode = replay` stanza's pacing keys.

---

## rawreplay (Piston) packs

Piston replays a recorded dataset **byte-for-byte**, re-timestamped to now — the
`splunk/security_content` `attack_data` use case. A rawreplay pack is recognised
(by both the linter and git-sync) from its `pack.yaml`: `engine: rawreplay`, **or**
a `replay:` section carrying a `dataset` / `dataset_url`. It has **no**
`default/eventgen.conf` requirement of its own — the `replay:` section replaces it.

### The `replay:` section (verified against `parse_replay_config`)

| Key | Required | Meaning |
|---|---|---|
| `dataset` | one of `dataset`/`dataset_url` | Pack-relative path to the recorded capture. Must stay inside the pack root (absolute paths and `..` traversal are rejected) and exist. |
| `dataset_url` | one of `dataset`/`dataset_url` | An **https** URL to a public host, fetched at build time **only when there is no local `dataset`**. See [safety](#dataset_url-safety). |
| `mode` | no (default `rate`) | `rate` or `cadence`. Any other value is a lint error. |
| `time_multiple` | no (default `1.0`) | Cadence gap stretch/compress factor; must be `> 0` at lint time. `1.0` = real time, `<1` faster, `>1` slower. |
| `dataset_sha256` | no | Lowercase hex sha256 that a fetched `dataset_url` must match (pinned dataset). |
| `ts_regex` | no | Cadence-mode timestamp hint (regex to find the event time). |
| `ts_strptime` | no | Cadence-mode `strptime` format for the captured timestamp. |
| `ts_field` | no | Cadence-mode field name holding the timestamp. |

`sourcetype` and `source` come from `defaults:` (a rawreplay pack has no eventgen
output-side keys). The agent stamps index/sourcetype/host/source from the run
slice exactly as for eventgen.

**`dataset` vs `dataset_url` precedence.** A local `dataset` always wins: when it
is present, any `dataset_url` beside it is **provenance only** (recorded, never
fetched, not host-checked). `dataset_url` is the actionable fetch source **only**
when the pack ships no local `dataset`.

### rate vs cadence

- **rate** (`mode: rate`; run rate_mode `eps` / `per_day_gb`): the engine emits
  events hot with `time = null`; the agent stamps *now* and its token bucket
  paces to the exact rate. The dataset **loops** to fill the run duration.
- **cadence** (`mode: cadence`; run rate_mode `count_interval`): the engine
  self-paces, reproducing the recorded inter-event gaps × `time_multiple` and
  setting `time = now + offset`. Not token-bucket gated (engine-paced) — which is
  why replay is pinned to one worker.

### Why a rawreplay pack ALSO ships `default/eventgen.conf`

A rawreplay pack does not *need* `default/eventgen.conf` to be recognised (the
`replay:` section is enough). Ship one anyway with a single `mode = replay`
stanza, because:

1. **Pack contract.** A pack is a well-formed eventgen pack too; tooling that
   expects `default/eventgen.conf` keeps working.
2. **Graceful fallback.** If the run is ever forced onto the eventgen engine,
   `mode = replay` streams the same dataset (eventgen's own replay), rather than
   failing.

**Piston never reads this conf.** For `engine == rawreplay` the agent skips the
eventgen conf-rewrite entirely; Piston reads its config from the bundle's
`stoker.json` (built from `pack.yaml`'s `replay:` section). Keep the conf's
`sampleFile` pointed at the same dataset for the fallback to be faithful.

### Minimal working example — `packs/attack-replay`

`pack.yaml`:

```yaml
name: attack-replay
engine: rawreplay
description: "Replays a recorded Sysmon/Windows-Security attack_data capture byte-for-byte, re-stamped to now"
replay:
  dataset: dataset/events.log
  mode: rate
  time_multiple: 1.0
  dataset_url: https://github.com/splunk/security_content/tree/develop/datasets/attack_techniques
estimates:
  bytes_per_event: 1591
  per_day_gb: 0.05
defaults:
  index: main
  sourcetype: XmlWinEventLog
```

`default/eventgen.conf` (the fallback stanza; skipped by Piston):

```ini
[attack-replay]
mode = replay
sampleFile = dataset/events.log
timeMultiple = 1.0
token.0.token = SystemTime='(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)'
token.0.replacementType = replaytimestamp
token.0.replacement = %Y-%m-%dT%H:%M:%S.%f000Z
```

Layout:

```
packs/attack-replay/
  pack.yaml               # name / engine: rawreplay / replay / estimates / defaults
  default/eventgen.conf   # one mode=replay stanza (contract + eventgen fallback)
  dataset/events.log      # the recorded capture Piston replays byte-for-byte
```

A `dataset_url`-only variant (no local dataset — the control plane fetches and
embeds the capture at build time):

```yaml
name: t1003-lsass
engine: rawreplay
description: "LSASS credential dump replay (fetched attack_data)"
replay:
  dataset_url: https://media.githubusercontent.com/media/splunk/attack_data/master/datasets/attack_techniques/T1003.001/atomic_red_team/windows-sysmon.log
  dataset_sha256: <64-hex-of-the-capture>
  mode: rate
estimates:
  bytes_per_event: 1600
defaults:
  index: main
  sourcetype: XmlWinEventLog
```

The fetched payload lands inside the bundle at `dataset/replay.dat`; the
manifest's `replay.dataset` points the worker there, so the worker never
re-fetches from the network.

### `dataset_url` safety

A `dataset_url` can originate from an untrusted synced repo, and its bytes are
embedded in a bundle then replayed to a HEC target, so the control-plane fetch is
guarded as both an SSRF sink and a read/exfiltration primitive
(`server/bundles._assert_fetchable_url` / `_fetch_dataset_url`):

- **https only.** A non-https scheme is refused.
- **Public host only.** The host is resolved and **every** address it resolves
  to must be global unicast. Refused: loopback (`127/8`, `::1`), link-local
  (`169.254/16`, incl. the cloud-metadata IP `169.254.169.254`), private
  (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`) and reserved/multicast ranges.
  The linter additionally rejects an obvious internal **IP literal** cheaply at
  index time (a hostname is left to the fetch-time resolve so flaky DNS does not
  fail lint).
- **No embedded credentials.** A `user:pass@host` URL is refused.
- **Redirects re-validated.** Auto-redirects are disabled; each hop (max 3) is
  re-checked against the same rules, so a public URL cannot 30x into an internal
  one.
- **Size cap.** The body streams and aborts past the cap
  (`RAWREPLAY_MAX_DATASET_BYTES`, default **512 MiB**); timeout
  `RAWREPLAY_FETCH_TIMEOUT_S`, default **120 s**.
- **sha pin.** When `dataset_sha256` is set, the fetched bytes must match or the
  build fails.

Residual: a sub-second DNS rebind between the resolve and the socket connect is
not defeated here; the practical holes (an internal literal, or a redirect to
one) are closed, and reaching this fetch already requires an operator to have
wired an untrusted repo plus a rawreplay pack.

> `attack_data` captures are large and stored via Git LFS. Point `dataset_url`
> at the `media.githubusercontent.com/media/...` raw URL (or vendor the file and
> use a local `dataset:`), **not** the GitHub HTML `/tree/` or `/blob/` page.

---

## Packs from a git repo (git-sync)

A repo (registered under `/api/repos`) is shallow-cloned, pinned to a SHA, and
walked for pack roots by `server/gitsync/sync.py`.

### Discovery — where packs must live

`_find_pack_roots` looks in exactly two places:

- the **repo root** (a repo that *is* a single pack), and
- each immediate subdirectory of **`packs/`** (`packs/<name>/`).

A directory is a pack root when it holds `default/eventgen.conf` (eventgen) **or**
its `pack.yaml` declares rawreplay (`engine: rawreplay` / a `replay:` section with
a dataset). Nested deeper than `packs/<name>/` is not discovered. This is the
**monorepo `packs/<name>/` layout** — the same layout as this repo's `packs/`.

### Per-pack indexing

For each discovered pack (`index_packs`):

- **Synthesised `pack.yaml`.** An eventgen pack with no `pack.yaml` gets a minimal
  one synthesised (with a measured `bytes_per_event`). A rawreplay pack always
  ships its own `pack.yaml` (that is how it is recognised) so it is never
  synthesised.
- **Lint** via `bundles.lint_pack` (same linter as local packs).
- **Custom-code default-deny.** A pack is rejected unless the repo is flagged
  `trusted_code` when it contains either a non-empty **`bin/`** directory
  (arbitrary Python eventgen would import and run in a worker) or any
  **`generator = <name>`** stanza where `<name>` is not `default` (a custom
  generator plugin). The `bin/` tree is never included in the bundle regardless.
- **Path-escape rejection.** Any `token.N.replacement` guarded by
  `replacementType = file|mvfile` whose path escapes the pack root is rejected
  (a malicious pack must not read arbitrary host files). The same containment
  applies to a rawreplay `replay.dataset` path.
- **Upsert** a `Pack` row keyed on `(repo_id, name)` with `indexed_sha` = the
  repo head SHA, `engines_json` (`["rawreplay"]` for a rawreplay pack),
  `lint_status`, `lint_errors_json` and `verified`.

### `trusted` vs `verified` — two independent flags

- **`repo.trusted_code`** (operator-set per repo): allows the custom-code
  constructs above (`bin/`, custom `generator =`). Without it, such packs fail
  lint. It is about *whether arbitrary code is permitted to run*.
- **`pack.verified`** (derived per pack): `true` only when the pack lints clean
  **and** shipped an author-supplied `pack.yaml` (not synthesised). It is about
  *whether the pack's metadata is author-declared*. A synthesised-`pack.yaml`
  pack, or any lint failure, is `verified = false`.

The two are orthogonal: a pack can be verified but from an untrusted repo (no
custom code), or trusted-code but unverified (synthesised metadata).

Credential handling for private repos (PAT via `GIT_ASKPASS`, deploy-key via
`GIT_SSH_COMMAND`, transports restricted to `https:ssh:git:file`, secrets never
on argv or in logs) is documented in `server/gitsync/sync.py`.

---

## The built bundle (what the worker consumes)

`server/bundles.build_from_pack` lints the pack, writes a `stoker.json` manifest,
and tars the pack **reproducibly** (sorted members, fixed mtime/uid/gid/mode,
zeroed gzip mtime) so an unchanged pack yields a byte-identical archive. The
tarball is sha256'd and stored content-addressed at `{BUNDLE_DIR}/<digest>.tgz`;
an identical pack dedups to the same digest.

The archive unpacks to `<pack-name>/…` (root-plus-one, which the worker's
`_find_pack_root` accepts). It contains `default/eventgen.conf` (when present),
everything under `samples/`, `pack.yaml`, the generated `stoker.json`, and — for
rawreplay — the dataset (a local `dataset:` in place, or a fetched `dataset_url`
at `dataset/replay.dat`).

`stoker.json` is exact JSON and is preferred by the worker over `pack.yaml`
(which carries the subset-parser caveats). For a rawreplay pack it carries a
`replay` block with the **bundle-relative** dataset path, `mode`, `time_multiple`,
`sourcetype`, `source` and any cadence `ts_*` hints. `dataset_url` and
`dataset_sha256` are **not** carried into the manifest — the dataset is already
embedded, so the worker never re-fetches:

```json
{
  "name": "attack-replay",
  "engine": "rawreplay",
  "estimates": {"bytes_per_event": 1591, "per_day_gb": 0.05},
  "stanzas": [],
  "sourcetypes": ["XmlWinEventLog"],
  "replay": {
    "dataset": "dataset/events.log",
    "mode": "rate",
    "time_multiple": 1.0,
    "sourcetype": "XmlWinEventLog",
    "source": null
  }
}
```

---

## Sourcing real datasets

The bundled eventgen packs (`web-access`, `aws-cloudtrail`, `aws-s3-access`,
`aws-elb-alb`, `apigw`, `flatline`) are **synthetic** — authored in-repo, so they
carry a clean Apache-2.0 licence, template to any volume and re-stamp to now. That
is the recommended default for a public pack.

When you want to replay a **real recorded capture** instead, add a
[`rawreplay` pack](#rawreplay-piston-packs) pointing `replay.dataset_url` at a
public dataset (or vendor the file and use a local `replay.dataset`). Mind the
[`dataset_url` safety rules](#dataset_url-safety) and, above all, the licence of
the source. Some useful public corpora, with their terms as of this writing
(**verify before redistributing — licences change**):

| Source | Contents | Format | Licence |
|---|---|---|---|
| [splunk/botsv3](https://github.com/splunk/botsv3) (also v1/v2) | 80+ sourcetypes: CloudTrail, VPC Flow, S3 access, GuardDuty, DNS, Sysmon, WinEventLog, Azure AD, O365 | Pre-indexed Splunk buckets + JSON export | **CC0** (public domain) |
| [splunk/attack_data](https://github.com/splunk/attack_data) | Attack captures by MITRE ATT&CK technique; Windows/Sysmon-heavy, some nginx/auditd/O365/CloudTrail | Raw log files (Git LFS) | **Apache-2.0** |
| [logpai/loghub](https://github.com/logpai/loghub) | 16+ system-log datasets: Apache, OpenSSH, Linux, Hadoop, HDFS, OpenStack, … | Raw log files | **Research-only** + citation (not for commercial redistribution) |
| [NASA-HTTP](https://ita.ee.lbl.gov/html/contrib/NASA-HTTP.html) / Calgary-HTTP (ITA) | Classic web-server access logs (NCSA combined) | Raw log files | "Freely redistributable" (1995 data) |
| [secrepo.com](https://www.secrepo.com/) | Curated security samples: web logs, network captures, threat intel | Mixed raw | **CC BY 4.0** (attribution) |
| [flaws.cloud CloudTrail](https://summitroute.com/blog/2020/10/09/public_dataset_of_cloudtrail_logs_from_flaws_cloud/) | 1.9M real CloudTrail events (attack traffic) | Gzipped CloudTrail JSON | **Unclear** — shared for community use; do not redistribute without asking the author |

Notes:

- **Best clean-licence real data:** BOTSv3 (CC0) and `attack_data` (Apache-2.0).
  BOTSv3 ships as pre-indexed Splunk buckets — extract raw events from its JSON
  export for replay; the buckets themselves are not directly replayable.
- **Splunk Technology Add-ons** (`Splunk_TA_aws`, `TA-apache_access_eventgen`,
  etc.) ship their own `default/eventgen.conf` + `samples/`, but under the Splunk
  Software Licence Agreement — fine to run locally, **not** to redistribute inside
  your own pack. Treat them as a format reference, not a data source to vendor.
- `attack_data` captures are large and stored via Git LFS: point `dataset_url` at
  the `media.githubusercontent.com/media/...` raw URL, not the GitHub HTML page
  (see the [`dataset_url` note](#rawreplay-piston-packs)).
