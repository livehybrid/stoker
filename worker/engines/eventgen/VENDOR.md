# Vendored splunk_eventgen

- Upstream: https://github.com/splunk/eventgen
- Tag: `7.2.1`
- Commit: `c952d13c99d5b768312ca60b20d824f60680cfe0`
- Licence: Apache-2.0, kept verbatim as `LICENSE.upstream`
- Scope: only the `generate` subcommand is supported. The controller/server REST plane, Redis clustering and Splunk app packaging are removed.

Runtime dependency surface of the vendored tree after the patches below: `jinja2` and `python-dateutil` (pins in `worker/requirements.txt`). The only other non-stdlib imports are `splunk.entity` / `splunk.auth`, which are lazy imports on Splunk-embedded code paths that are unreachable when running standalone.

## Deletions

| Path | Reason |
|---|---|
| `eventgen_api_server/` (all 6 modules) | Flask/Redis controller+server REST plane, not used by `generate` |
| `splunk_app/` | SA-Eventgen Splunk app scaffold, only consumed by the removed `build` subcommand |
| `identitygen.py` | Standalone identity CSV helper, imported by nothing on the generate path |
| `lib/requirements.txt` | Stale upstream pins (ujson 2.0.3, jinja2 2.10.3, requests-futures, urllib3 1.24.2, six); superseded by `worker/requirements.txt` |
| `lib/plugins/output/awss3.py` | boto3/botocore/requests-futures |
| `lib/plugins/output/httpevent.py` | ujson, HEC is the agent's job |
| `lib/plugins/output/httpevent_core.py` | requests/requests-futures/ujson/six |
| `lib/plugins/output/metric_httpevent.py` | ujson, depends on deleted httpevent_core |
| `lib/plugins/output/scsout.py` | requests-futures/ujson, SCS ingest not used |
| `lib/plugins/output/splunkstream.py` | httplib2/six/splunk.auth |
| `lib/plugins/output/tcpout.py` | dead network destination; the engine must not own transports |
| `lib/plugins/output/udpout.py` | dead network destination |
| `lib/plugins/output/s2s.py` | Splunk-to-Splunk protocol, dead destination |
| `lib/plugins/output/syslogout.py` | dead network destination |
| `lib/plugins/generator/weblog.py` | broken by construction: hardcoded `open("tests/sample_eventgen_conf/perf/weblog/...")` paths into the upstream tests tree, which is not vendored |

No Dockerfiles existed inside `splunk_eventgen/` (upstream keeps them in a top-level `dockerfiles/` directory that was never copied).

Output plugins kept (all stdlib-only): `stdout`, `devnull`, `file`, `spool`, `modinput` (the packaged `default/eventgen.conf` default), `counter`.
Generator plugins kept: `default`, `replay`, `jinja`, `windbag`, `counter`, `perdayvolumegenerator`. All raters kept.

## Patches

| File | Patch | Reason |
|---|---|---|
| `__main__.py` | removed `service` subcommand and its Flask/Redis imports, `build` subcommand plus `build_splunk_app`/`make_tarfile`/`filter_function`, `gather_env_vars`, the py<3.8 `importlib_metadata` fallback and the dead `args.print_version()` branch | generate-only CLI with no server or packaging imports |
| `eventgen_core.py` | `imp` → `importlib.util` in `_initializePlugins` (module still registered under its basename in `sys.modules`) | `imp` was removed in py3.12; registration parity keeps multiprocess pickling working |
| `eventgen_core.py` | `worker.setDaemon(True)` → `worker.daemon = True` (4 sites), `stop_request.isSet()` → `is_set()` (3 sites) | camelCase threading aliases are deprecated/removed in newer pythons |
| `eventgen_core.py` | `self.logger.ERROR(...)` → `self.logger.error(...)` | upstream AttributeError bug in `kill_processes` |
| `eventgen_core.py` | explicit `import logging.handlers` | `logging.handlers.QueueHandler` was reached only via a side effect of dictConfig importing the submodule |
| `lib/eventgenconfig.py` | `six.moves.urllib` imports → `urllib.request`; `pathname2url` call updated | drop six |
| `lib/eventgensamples.py` | same six → `urllib.request` swap in `saveState` | drop six |
| `lib/eventgentoken.py` | six imports → `urllib.parse`; `quote` call updated | drop six |
| `lib/eventgentoken.py` | `random.randint(minDelta, maxDelta)` → `int(maxDelta)` cast | float args to randint raise on py3.10+ |
| `lib/eventgentoken.py` | `open(replacementFile, "rU")` → `open(replacementFile, "r")` on the `file`/`mvfile` token path | the `U` (universal-newlines) mode flag was removed in py3.11 and raises `ValueError: invalid mode: 'rU'`; text mode is universal-newlines by default in py3, so behaviour is unchanged. Without this, any pack with a `replacementType = file` token (`apigw`, `web-access`, `aws-s3-access`, `aws-elb-alb`) fails to render on py3.11+ |
| `lib/generatorplugin.py` | httplib2 + six backfillSearch REST call → stdlib `urllib.request` with unverified SSL context (matches upstream's disabled cert validation) | drop httplib2 and six |
| `lib/generatorplugin.py` | undefined name `c` → `self.config` in `setupBackfill`, catch `AttributeError` too | upstream NameError bug (was flake8-suppressed with noqa F821) |
| `lib/eventgentimestamp.py` | `int()` casts on `time.mktime` results fed to `random.randint` (3 sites) | float args to randint raise on py3.10+ |
| `lib/raterplugin.py` | `random.randint(0, int(randBound))` | `round(x, 0)` returns float; same randint constraint |
| `lib/plugins/generator/jinja.py` | removed `"jinja2.ext.with_"` from the extension list | extension removed in jinja2 3.0, `with` is built in |
| `lib/plugins/generator/jinja.py` | `template.stream()` iteration → `template.render().splitlines()` | jinja2 3.x streams one chunk per output node, not per line; upstream relied on 2.x merging a whole line into one chunk |
| `lib/plugins/generator/jinja.py` | ujson try/except → plain `import json` | ujson removed from the dependency set |
| `lib/plugins/generator/jinja.py` | `e.message` → `str(e)` | py3 exceptions have no `.message` |
| `lib/plugins/generator/jinja.py` | `jinja_loaded_vars = None` → `{}` when `jinja_variables` is unset | upstream crash: the dict is subscripted unconditionally right after |

## Additions

| File | Purpose |
|---|---|
| `lib/plugins/output/stoker.py` | Stoker output plugin (not upstream): `outputMode = stoker` streams every event as an NDJSON envelope over the unix socket in `STOKER_OUTPUT_SOCKET` to the worker agent. Stdlib only. The file must keep this name: the plugin registry key is `output.<filename stem>`. Tested by `worker/tests/test_stoker_output.py`. |

## Verification

- Every file byte-compiles on py3.12 (`python -m compileall`).
- No remaining imports of any deleted module (AST-scanned, enforced by `worker/tests/test_eventgen_vendor.py`).
- Smoke-verified on py3.12 with `jinja2==3.1.6`: windbag, default generator with timestamp token, jinja generator with a template, replay mode with replaytimestamp token, perDayVolume rater with file output and randomizeCount rating.
