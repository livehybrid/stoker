# Engine benchmarks: firebox vs the vendored Python eventgen

Measured 2026-09-26 with `tools/bench_engines.py` on the AIOS development box
(Intel i7-4770HQ, 4 cores / 8 threads, host load average 8 to 10 from other
work; +/-20% noise, ratios are what matter). Pack `packs/web-access`
(access_combined, 3 tokens incl. a file token). Each row is one standalone
worker (`python -m stoker_agent`, `STOKER_RATE_MODE=eps`, 20 s) delivering to
`tools/hec_sink.py`.

```
FIREBOX_BIN=/path/to/firebox .venv/bin/python tools/bench_engines.py \
    --pack packs/web-access --rates 2000,5000,10000,20000 --duration 20
```

| engine | target eps | delivered eps | achieved | agent+engine CPU (cores) |
|---|---|---|---|---|
| python eventgen | 2,000 | 690 | 35% | 0.94 |
| python eventgen | 5,000 | 288 | 6% | 0.96 |
| python eventgen | 10,000 and above | 0 within 20 s | 0% | 0.94 |
| firebox | 2,000 | 2,000 | 100% | 0.71 |
| firebox | 5,000 | 4,731 | 95% | 1.04 |
| firebox | 10,000 | 4,351 | 44% | 0.98 |
| firebox | 20,000 | 3,482 | 17% | 0.74 |

What the numbers say:

- **The Python engine could not reach even a 2,000 eps share** on this box
  (a third of it), and above 5,000 it never finished templating its first
  interval's batch inside the run, because it renders a whole interval before
  flushing anything.
- **firebox delivers the share exactly** up to about 4,700 eps per worker
  with the classic envelope, then flattens. That flat line is the **agent**:
  with a Rust engine feeding it, the Python reader thread (JSON-decode each
  envelope, token bucket, re-serialise for HEC, gzip) saturates one core at
  4.5 to 4.7k events/s. The HEC-line envelope below lifts that to ~15k.
- The raw engine, without the agent, templates this pack at ~105k events/s per
  thread (linear to the cores the box can spare): the engine is no longer a
  factor in per-worker throughput. See the firebox repo's `BENCHMARKS.md`.
- One engine connection per generator thread made things worse (1.9k eps):
  eight reader threads starve the HEC senders for the GIL. firebox therefore
  shares one connection by default (`FIREBOX_SOCKET_CONNECTIONS=single`).

## With the HEC-line envelope (the default with firebox)

The agent's per-event decode/fill/re-encode was that wall, so firebox now
emits final HEC objects (`STOKER_ENVELOPE=hec`, metadata policy in
`STOKER_ENVELOPE_META`, both set by the agent) and the reader only paces and
forwards bytes. Same harness, same pack:

| engine | target eps | delivered eps | achieved | agent+engine CPU (cores) |
|---|---|---|---|---|
| firebox (hec envelope) | 5,000 | 4,998 | 100% | 0.83 |
| firebox (hec envelope) | 10,000 | 10,000 | 100% | 1.07 |
| firebox (hec envelope) | 20,000 | 11,895 | 59% | 1.02 |
| firebox (hec envelope) | 50,000 | 15,343 | 31% | 1.28 |
| firebox (hec envelope) | 100,000 | 11,060 | 11% | 1.01 |

Exact delivery to 10,000 eps per worker, ceiling ~15,000: three times the
classic envelope and over twenty times the Python engine. `STOKER_FAST_ENVELOPE=0`
restores the classic envelope (the Python engine always uses it). The next
wall is the agent's per-line token bucket and queue hand-off (~65 us per event
on one core) plus its HEC senders; the fix for that is batching inside the
agent.

## Batched agent (2026-09-27)

The HEC-line path now paces and hands off each socket read as one batch: one
token-bucket grant (`acquire_many`, never more than the wall clock already
owes), one queue item and one counter update per chunk instead of per event.
Micro-benchmarks put the per-event costs it removes at about 10 us (bucket),
18 us (queue) and 4 us (counter lock). Measured A/B with the count-only
`tools/fast_sink.py` (the validating `hec_sink.py` parses every event in
Python and became the bottleneck above ~12k eps), same pack, host load
average 20 to 26 on 8 threads, so absolute numbers are pessimistic:

| agent | target eps | delivered eps | achieved | CPU per event |
|---|---|---|---|---|
| before batching | 20,000 | 9,264 | 46% | 117 us |
| before batching | 100,000 | 9,509 | 10% | 116 us |
| batched | 10,000 | 10,000 | 100% | 112 us |
| batched | 20,000 | 19,166 | 96% | 66 us |
| batched | 50,000 | 24,360 | 49% | 56 us |
| batched | 100,000 | 31,332 | 31% | 55 us |

About 3.3 times the per-worker ceiling and half the CPU per event. Pacing
accuracy is unchanged (tests assert the delivered rate against the bucket).
`tools/bench_engines.py --sink fast` is now the default.

Consequences for operating Stoker:

- The 5,000 eps default per-worker ceiling (`STOKER_MAX_EPS_PER_WORKER`) is
  now comfortably inside what one worker delivers; on a box like this it can
  be raised to ~10,000 with exact delivery. Fleets scale horizontally as
  before, and every worker actually hits its share.
- `STOKER_EVENTGEN_IMPL=python` restores the old engine per worker if a pack
  ever needs something firebox does not implement (see the firebox `COMPAT.md`).

## Live soak (2026-09-27)

`tools/soak_live.py` drives the live control plane (stack 107, image 1cf5384)
through its operator API: swarm workers, firebox, the batched agent and the
HEC client, into Splunk 10 on 192.168.0.222 (`index=loadtest`), pack
`nginx-access` (~348 bytes/event on the wire), 120 s per step. "Indexed" is
Splunk's count for the step's unique `source`.

| target eps | workers | delivered eps | achieved | indexed | HEC 4xx/5xx/timeouts | sender queue max | worker CPU |
|---|---|---|---|---|---|---|---|
| 2,000 | 1 | 2,000 | 100% | 240,002 (all) | 0/0/0 | low | low |
| 5,000 | 1 | 5,000 | 100% | 599,992 (all) | 0/0/0 | low | low |
| 10,000 | 1 | 9,988 | 99.9% | 1,198,503 (all) | 0/0/0 | 4,954 | 46% |
| 20,000 | 2 | 15,469 | 77.3% | 1,856,275 (all) | 0/0/0 | 5,000 (full) | 10-23% |

Every delivered event was indexed and HEC never returned an error. The 20k
step was bound by the **target**, not the workers: both senders' queues sat
at their cap while CPU stayed low, i.e. the agents waited on HEC round-trips.
This Splunk (a single instance sharing an 8-core host running at load ~24)
absorbs about 15.5k eps (~5.4 MB/s) over HEC.

Conclusions:
- The eventgen default of **10,000 eps per worker** is validated end to end on
  the real fleet; the fast-sink measurement (~31k eps/worker) is the agent's own
  ceiling and needs a faster target to reach.
- Past ~10k eps against one modest Splunk, add HEC capacity (more indexers /
  HEC endpoints behind a load balancer), not workers.
- A full sender queue with zero HEC errors is the signature of a slow target;
  it is the signal the target-health backpressure in issue #2 should watch
  (sustained full queue or rising lag), alongside 429/5xx.
