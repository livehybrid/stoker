"""Control plane client and fencing state.

ControlClient speaks the managed-mode protocol against
{CONTROL_URL}/api/agent/runs/{run_id}/ with a bearer JWT that can be
rolled by heartbeat responses. StandaloneControl is the no-control-plane
stub: heartbeat lines go to stdout and the first heartbeat returns a
release with T0 = now + 2 s.

Fencing per the contract: a successful heartbeat ack is the lease renewal.
30 s without one means pause generation; STOKER_DEADMAN_S without one means
drain and exit 4. A superseded response is a fatal drain.
"""

from __future__ import annotations

import json
import logging
import random
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests

from .slice import format_iso8601

log = logging.getLogger("stoker.control")

PROTOCOL_VERSION = 1
FENCE_PAUSE_S = 30.0

BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 30.0


class ControlError(Exception):
    """Unrecoverable control plane failure."""


class SupersededError(ControlError):
    """The control plane replaced this worker's lease: fatal drain."""


class DeadManError(ControlError):
    """No successful control plane contact within the dead-man window."""


class ControlClient(object):
    def __init__(
        self,
        control_url,      # type: str
        run_id,           # type: Any
        jwt,              # type: str
        deadman_s=600.0,  # type: float
        session=None,     # type: Optional[requests.Session]
        clock=time.monotonic,   # type: Callable[[], float]
        sleep=time.sleep,       # type: Callable[[float], None]
        request_timeout_s=10.0,  # type: float
    ):
        self._base = control_url.rstrip("/") + "/api/agent/runs/%s" % run_id
        self._jwt = jwt
        self._deadman_s = float(deadman_s)
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._timeout = request_timeout_s
        self._lock = threading.Lock()
        # Grace window starts at construction so a worker that never reaches
        # the control plane still hits the dead-man.
        self._last_ack = clock()

    # -- transport -------------------------------------------------------

    def _post(self, path, body, timeout=None):
        # type: (str, Dict[str, Any], Optional[float]) -> Dict[str, Any]
        """One attempt. Raises requests exceptions / ControlError on failure."""
        with self._lock:
            headers = {"Authorization": "Bearer " + self._jwt}
        resp = self._session.post(
            self._base + "/" + path, json=body, headers=headers,
            timeout=self._timeout if timeout is None else timeout,
        )
        if resp.status_code >= 400:
            raise ControlError("control %s returned HTTP %d" % (path, resp.status_code))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise ControlError("control %s returned unparseable JSON" % path)

    def _post_with_backoff(self, path, body):
        # type: (str, Dict[str, Any]) -> Dict[str, Any]
        """Retry with exponential backoff and jitter until success or dead-man."""
        start = self._clock()
        attempt = 0
        while True:
            try:
                return self._post(path, body)
            except (requests.exceptions.RequestException, ControlError) as exc:
                if isinstance(exc, SupersededError):
                    raise
                elapsed = self._clock() - start
                if elapsed >= self._deadman_s:
                    raise DeadManError(
                        "control %s failed for %.0f s (dead-man %.0f s): %s"
                        % (path, elapsed, self._deadman_s, exc))
                delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** attempt))
                delay *= random.uniform(0.5, 1.5)
                log.warning("control %s failed (%s); retry in %.1f s",
                            path, exc, delay)
                self._sleep(delay)
                attempt += 1

    # -- protocol --------------------------------------------------------

    def claim(self, holder, hint_slot=None):
        # type: (str, Optional[int]) -> Dict[str, Any]
        body = {"holder": holder, "protocol_version": PROTOCOL_VERSION}
        if hint_slot is not None:
            body["hint_slot"] = hint_slot
        doc = self._post_with_backoff("claim", body)
        self._record_ack()
        return doc

    def ready(self, slot, lease_id):
        # type: (int, Optional[str]) -> None
        self._post_with_backoff("ready", {"slot": slot, "lease_id": lease_id})
        self._record_ack()

    def heartbeat(self, payload):
        # type: (Dict[str, Any]) -> Optional[Dict[str, Any]]
        """One heartbeat attempt (the run loop provides the cadence).

        Returns the response document on success, None on a missed ack.
        Raises SupersededError when the lease has been taken over. Handles
        rolling JWT refresh from the response.
        """
        body = dict(payload)
        body.setdefault("protocol_version", PROTOCOL_VERSION)
        try:
            doc = self._post(body=body, path="heartbeat")
        except (requests.exceptions.RequestException, ControlError) as exc:
            log.warning("heartbeat missed: %s", exc)
            return None
        new_jwt = doc.get("jwt")
        if new_jwt:
            with self._lock:
                self._jwt = new_jwt
            log.info("rolled run JWT from heartbeat response")
        if doc.get("command") == "superseded":
            raise SupersededError("lease superseded by the control plane")
        self._record_ack()
        return doc

    def final(self, slot, summary, log_tail, deadline=None):
        # type: (int, Dict[str, Any], list, Optional[float]) -> bool
        """Best-effort final POST; never blocks exit past `deadline`.

        `deadline` is a monotonic-clock instant (the agent's drain deadline);
        each attempt's request timeout is clamped to the time left and no
        attempt starts once it has passed, so a dead control plane cannot push
        the drain over the SIGTERM budget.
        """
        body = {"slot": slot, "summary": summary, "log_tail": log_tail}
        for attempt in range(3):
            timeout = self._timeout
            if deadline is not None:
                left = deadline - self._clock()
                if left <= 0:
                    break
                timeout = min(self._timeout, left)
            try:
                self._post("final", body, timeout=timeout)
                return True
            except (requests.exceptions.RequestException, ControlError) as exc:
                log.warning("final POST attempt %d failed: %s", attempt + 1, exc)
                if deadline is not None and self._clock() >= deadline:
                    break
                self._sleep(min(2.0, BACKOFF_BASE_S * (2 ** attempt)))
        return False

    # -- fencing ----------------------------------------------------------

    def _record_ack(self):
        with self._lock:
            self._last_ack = self._clock()

    def seconds_since_ack(self):
        # type: () -> float
        with self._lock:
            return self._clock() - self._last_ack

    def should_pause(self):
        # type: () -> bool
        return self.seconds_since_ack() > FENCE_PAUSE_S

    def deadman_expired(self):
        # type: () -> bool
        return self.seconds_since_ack() > self._deadman_s


class StandaloneControl(object):
    """Control stub for STOKER_STANDALONE=1.

    Heartbeat lines are logged to stdout; the first heartbeat poll returns a
    release with T0 = now + 2 s. Fencing and the dead-man never trigger:
    there is no control plane to lose.
    """

    RELEASE_DELAY_S = 2.0

    def __init__(self, clock=time.time, out=None):
        # type: (Callable[[], float], Optional[Any]) -> None
        self._clock = clock
        self._out = out or sys.stdout
        self._released = False

    def claim(self, holder, hint_slot=None):
        raise ControlError("standalone mode has no claim endpoint")

    def ready(self, slot, lease_id):
        self._out.write("[stoker] ready slot=%s\n" % slot)
        self._out.flush()

    def heartbeat(self, payload):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        body = dict(payload)
        body.setdefault("protocol_version", PROTOCOL_VERSION)
        self._out.write("[stoker] heartbeat %s\n"
                        % json.dumps(body, sort_keys=True, default=str))
        self._out.flush()
        if not self._released:
            self._released = True
            t0 = self._clock() + self.RELEASE_DELAY_S
            return {"command": "release", "t0": format_iso8601(t0)}
        return {"command": "continue"}

    def final(self, slot, summary, log_tail, deadline=None):
        # type: (int, Dict[str, Any], list, Optional[float]) -> bool
        self._out.write("[stoker] final %s\n"
                        % json.dumps({"slot": slot, "summary": summary},
                                     sort_keys=True, default=str))
        self._out.flush()
        return True

    def seconds_since_ack(self):
        return 0.0

    def should_pause(self):
        return False

    def deadman_expired(self):
        return False
