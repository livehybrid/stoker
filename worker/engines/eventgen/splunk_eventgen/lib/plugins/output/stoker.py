"""Stoker output plugin: streams every generated event to the worker agent
over a unix stream socket (STOKER_OUTPUT_SOCKET) as one NDJSON envelope per
line: {"time", "host", "source", "sourcetype", "index", "event"}.

The engine constructs a NEW plugin instance for every flushed batch
(lib/eventgenoutput.py Output.flush), so the connection lives at module
level. With useOutputQueue = False the flush runs inline on each of the
generator worker threads; all writes serialise on one lock so a blocking
sendall while the agent stalls backpressures generation directly.
"""

import json
import math
import os
import socket
import threading

from splunk_eventgen.lib.logging_config import logger
from splunk_eventgen.lib.outputplugin import OutputPlugin

DEFAULT_SOCKET_PATH = "/tmp/stoker-output.sock"


class StokerSocketError(BaseException):
    """Deliberately a BaseException: Output.bulksend and the generator
    worker loop both catch bare Exception, which would swallow a dead
    socket and silently drop every subsequent batch."""


class _Connection(object):
    """Per-PROCESS engine socket. `dead` is sticky: once the stream fails
    the plugin never reconnects or resumes (the agent restarts the whole
    engine), so no batch can ever be dropped quietly.

    Per-process, not merely engine-wide, because eventgen's
    ``threading = process`` mode FORKS a generator process per worker. A fork
    copies this module's state, so a child would inherit the parent's already
    connected socket object and two processes would interleave writes into one
    stream (corrupting NDJSON), or inherit a sticky ``dead`` and refuse to run.
    ``ensure`` therefore records the owning pid and reconnects from scratch when
    it finds itself in a different process -- giving every generator process its
    own connection, which the agent's listener accepts concurrently.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.sock = None
        self.dead = False
        self.pid = None

    def ensure(self):
        """Return the connected socket, connecting on first use.
        Caller must hold self.lock."""
        if self.pid is not None and self.pid != os.getpid():
            # Inherited across a fork: drop the parent's socket (do NOT close
            # it -- the parent is still using that fd) and start our own.
            self.sock = None
            self.dead = False
            self.pid = None
        if self.dead:
            raise StokerSocketError(
                "stoker output socket previously failed; refusing to run"
            )
        if self.sock is None:
            path = os.environ.get("STOKER_OUTPUT_SOCKET", DEFAULT_SOCKET_PATH)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(path)
            except OSError as exc:
                sock.close()
                self.dead = True
                logger.error(
                    "stoker output: cannot connect to agent socket %s: %s",
                    path,
                    exc,
                )
                raise StokerSocketError(
                    "cannot connect to agent socket {0}: {1}".format(path, exc)
                )
            self.sock = sock
            self.pid = os.getpid()
        return self.sock

    def fail(self, exc):
        """Mark the connection dead and raise. Caller must hold self.lock."""
        self.dead = True
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        logger.error("stoker output: agent socket write failed: %s", exc)
        raise StokerSocketError(
            "agent socket write failed: {0}".format(exc)
        )

    def reset(self):
        """Forget all state (test hook; the engine never calls this)."""
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock = None
            self.dead = False
            self.pid = None


_CONNECTION = _Connection()


def _epoch_or_none(value):
    """Map eventgen's _time to the envelope's time field. _time is an int
    epoch from the default/perdayvolume/windbag paths, a float from replay
    and template-supplied (any JSON type) from jinja."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(result):
        return None  # NaN/inf would serialise as invalid JSON
    return result


def _text_or_none(value):
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _encode(item):
    """One queue item -> one UTF-8 NDJSON line. Real item shape (see
    lib/generatorplugin.py replace_tokens and lib/eventgenoutput.py send):
    _raw always present; index/host/source/sourcetype/_time/hostRegex
    usually present but not guaranteed on the jinja path."""
    raw = item["_raw"]
    if not isinstance(raw, str):
        raw = str(raw)
    envelope = {
        "time": _epoch_or_none(item.get("_time")),
        "host": _text_or_none(item.get("host")),
        "source": _text_or_none(item.get("source")),
        "sourcetype": _text_or_none(item.get("sourcetype")),
        "index": _text_or_none(item.get("index")),
        "event": raw.rstrip("\r\n"),
    }
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    try:
        return (text + "\n").encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates from binary-ish sample data: fall back to ASCII
        # escapes rather than kill the stream or drop the batch.
        text = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


class StokerOutputPlugin(OutputPlugin):
    name = "stoker"
    # Batch size when the conf leaves maxQueueLength = 0 (the default).
    MAXQUEUELENGTH = 1000
    # Inline flush on the generator worker thread: a blocked write stalls
    # generation, which is the whole backpressure design.
    useOutputQueue = False

    def __init__(self, sample, output_counter=None):
        OutputPlugin.__init__(self, sample, output_counter)

    def flush(self, q):
        # Encode the whole batch into one buffer and write it with a SINGLE
        # sendall, rather than one sendall syscall per event. On the single
        # GIL-bound generator thread the per-event syscall was a real ceiling on
        # per-worker EPS; one write per flush (up to MAXQUEUELENGTH events) cuts
        # that overhead. Backpressure is unchanged: a blocked write of the whole
        # buffer still stalls generation, and the agent reader splits the stream
        # on newlines so a multi-line write parses identically. Encoding is done
        # outside the lock (CPU only); only the socket write holds it.
        buf = b"".join(_encode(item) for item in q)
        if not buf:
            return
        conn = _CONNECTION
        with conn.lock:
            sock = conn.ensure()
            try:
                sock.sendall(buf)
            except OSError as exc:
                conn.fail(exc)


def load():
    """Returns the plugin class (engine calls this at discovery)."""
    return StokerOutputPlugin
