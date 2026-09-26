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
- **firebox delivers the share exactly** up to about 4,700 eps per worker,
  then flattens. That flat line is the **agent**: with a Rust engine feeding
  it, the Python reader thread (JSON-decode each envelope, token bucket,
  re-serialise for HEC, gzip) saturates one core at 4.5 to 4.7k events/s.
- The raw engine, without the agent, templates this pack at ~105k events/s per
  thread (linear to the cores the box can spare): the engine is no longer a
  factor in per-worker throughput. See the firebox repo's `BENCHMARKS.md`.
- One engine connection per generator thread made things worse (1.9k eps):
  eight reader threads starve the HEC senders for the GIL. firebox therefore
  shares one connection by default (`FIREBOX_SOCKET_CONNECTIONS=single`).

Consequences for operating Stoker:

- The 5,000 eps default per-worker ceiling (`STOKER_MAX_EPS_PER_WORKER`) is
  now an honest agent limit on a box like this; fleets scale horizontally as
  before, but every worker actually hits its share.
- Raising per-worker throughput further means moving the agent's per-event
  work out of Python (have firebox emit final HEC lines so the reader only
  paces and forwards bytes), tracked as the next step.
- `STOKER_EVENTGEN_IMPL=python` restores the old engine per worker if a pack
  ever needs something firebox does not implement (see the firebox `COMPAT.md`).
