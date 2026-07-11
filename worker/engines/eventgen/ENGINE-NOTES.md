# Eventgen engine notes for the agent builder

Everything below was read out of the vendored 7.2.1 source (paths relative to `worker/engines/eventgen/splunk_eventgen/`). It is the behaviour the agent can rely on.

## Invoking generate

```
EVENTGEN_LOG_DIR=/path/to/writable/dir \
PYTHONPATH=/path/to/worker/engines/eventgen \
python -m splunk_eventgen generate /workdir/rewritten/default/eventgen.conf
```

- `PYTHONPATH` must contain the directory that holds the `splunk_eventgen` package (`worker/engines/eventgen`). Nothing is pip-installed.
- `EVENTGEN_LOG_DIR` is mandatory in practice: `lib/logging_config/__init__.py` runs `dictConfig` at import time and opens five `RotatingFileHandler` files eagerly in that directory (default is `<package>/logs`, which is read-only in the image). The directory must exist and be writable before the interpreter imports the package or the process dies on import.
- Useful flags: `-v` / `-vv` before the subcommand raise log level to INFO/DEBUG (default ERROR); `generate -s <stanza>` runs one stanza only; `--multiprocess` exists but the worker must not use it (thread mode is the tested path).
- The engine always exits 0 via `sys.exit(0)` when all bounded samples finish. Unbounded samples (`end = -1`, the packaged global default is interval-driven with no end) run until killed.

## Conf and sample path resolution (lib/eventgenconfig.py)

- Two conf layers are always read, in order: the packaged `splunk_eventgen/default/eventgen.conf` (global defaults: `outputMode = modinput`, `count = -1`, `interval = 60`, `generator = default`, `rater = config`, `maxQueueLength = 0`, `useOutputQueue = false` ...), then the conf you pass. Your stanza values win.
- Pass the conf as a FILE path. If `sampleDir` is not set in the conf, the samples directory is `dirname(dirname(conffile))/samples`, so `<pack>/default/eventgen.conf` resolves to `<pack>/samples`. Passing a directory instead resolves samples against the directory's PARENT (`dirname(<dir>)/samples`), an upstream quirk, so do not do that.
- If `sampleDir` is set and relative, it is joined to `dirname(conffile)`, i.e. relative to `<pack>/default/`, not the pack root.
- A stanza name is a REGEX matched against every filename in the sample dir and the match must span the entire filename. Stanzas using `generator = default` or `replay` are silently dropped (log warning "in config but no matching files") when nothing matches. Stanzas with other generators (windbag, jinja, counter, perdayvolumegenerator) are kept without a file.
- When a stanza sets an explicit `sampleDir`, the config forces `maxIntervalsBeforeFlush = 1` and `maxQueueLength = maxQueueLength or 1` (flush per event). Without it, `maxQueueLength = 0` means "use the output plugin's `MAXQUEUELENGTH`".
- `perDayVolume` in a stanza silently rewrites `rater = perdayvolume`, `generator = perdayvolumegenerator`, `count = 1`. `mode = replay` rewrites `generator = replay`, `count = 1` and clears the rate shapers.

## outputMode → plugin mapping (eventgen_core.py `_initializePlugins`, lib/plugins/output/)

- At startup the engine scans `lib/plugins/{output,generator,rater}/*.py` (skipping `_*`), imports each file with importlib, registers it in `sys.modules` under its bare basename and calls the module-level `load()`.
- The registry key is `"<plugintype>." + <filename stem>`. `outputMode = stoker` therefore selects `lib/plugins/output/stoker.py`, so the stoker plugin file must be named `stoker.py` (the class-level `name` attribute is informational only).
- If a mode is not found in the registry the engine raises `PluginNotLoaded` and retries from `<sampleDir>/../bin/<name>.py` and `<sampleDir>/../lib/plugins/output/`, so a pack could theoretically ship its own plugin; the stoker plugin ships inside the vendored tree instead.

### Output plugin registration API

```python
from splunk_eventgen.lib.outputplugin import OutputPlugin

class StokerOutputPlugin(OutputPlugin):
    name = "stoker"          # informational
    MAXQUEUELENGTH = 10      # batch size used when the conf leaves maxQueueLength = 0
    useOutputQueue = False   # False = flush runs inline in the generator worker thread

    def __init__(self, sample, output_counter=None):
        OutputPlugin.__init__(self, sample, output_counter)

    def flush(self, q):
        ...

def load():
    return StokerOutputPlugin
```

- `flush(q)` receives a list of event dicts. Guaranteed key: `_raw` (str). Usually present: `index`, `host`, `source`, `sourcetype`, `_time` (int epoch from the default generator, float from replay, template-supplied from jinja) and sometimes `hostRegex`. Use `.get()` for everything except `_raw`.
- Lifecycle (lib/eventgenoutput.py `Output.flush`): a NEW plugin instance is constructed for EVERY batch (`outputPlugin(sample, output_counter)` then `updateConfig(config)`, `set_events(q)`, `run()`; `run()` calls your `flush(events)` then `_output_end()`). Any persistent state, in particular the unix socket connection, must live at module or class level, not on the instance, or you reconnect per batch.
- Optional class attributes read at discovery time and merged into config validation: `validSettings`, `defaultableSettings`, `intSettings`, `floatSettings`, `boolSettings`, `jsonSettings`, `complexSettings`.
- `useOutputQueue = False` (and the packaged global default `useOutputQueue = false`) means `flush` executes inline on the generator worker thread. A blocking socket write therefore stalls generation directly, which is exactly the backpressure the worker contract wants. `useOutputQueue = True` would route batches through the single OutputThread instead.

## Threading model (thread mode, the only mode the worker uses)

- Main thread: parse conf, load plugins, build pools, enqueue one `Timer` per sample, then poll queues every 5 s (`join_process`) until all bounded samples finish, then `stop()` and `sys.exit(0)`.
- 100 daemon timer threads (`TimeThreadN`) consume the sample queue; each runs `Timer.real_run()` which loops per interval: the rater computes the count for this interval and puts one generator work item on the worker queue.
- 20 daemon generator worker threads consume the worker queue (bounded, `--generator-queue-size`, default 500) and run the generator plugin: read sample lines, replace tokens, `Output.send/bulksend`, flush batches of `maxQueueLength` events into the output plugin.
- 1 daemon output thread services plugins with `useOutputQueue = True` (not the stoker plugin).
- When the socket stalls, generator threads block in `flush`, the worker queue fills, and the rater then drops whole intervals with a logged warning ("Generator queue full. Skipping current generation."). Combined with the agent's 15 % overdrive this is the designed slow-down path; the agent's wall-clock token bucket owns accuracy.

## SIGTERM behaviour

The engine installs NO signal handlers (`signal` is only used to SIGKILL child processes in the unused multiprocess mode). SIGTERM takes the default disposition: the process dies immediately, all worker threads are daemons, nothing flushes. Consequences for the agent:

- a partial final NDJSON line on the socket is possible; the reader must tolerate a truncated last line
- there is no engine-side drain: the agent drains its own queues after the engine is dead
- SIGTERM then a short wait then SIGKILL is a safe stop sequence; expect no exit-code discipline from the engine on signals (0 only on natural completion)
