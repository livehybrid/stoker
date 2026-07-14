"""HTTP clients for the Stoker integration harness.

Two thin, dependency-light wrappers (``requests`` only):

* :class:`StokerClient` -- the operator API, authenticated with an ``stk_`` bearer
  token (see the harness README for how to mint one). Every call returns the raw
  ``requests.Response``; :meth:`StokerClient.ok` asserts the status and returns the
  parsed JSON so tests stay terse.
* :class:`Splunk` -- a oneshot search client used to assert what actually landed
  in the index (events via ``stats count``, metric points via ``mstats``). Polls
  briefly so a test does not flake on indexing latency.

Nothing here is Stoker-internal: the harness drives the deployed app over HTTP
exactly as an external caller (or CI job) would.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class StokerClient:
    """Operator-API client. All paths are absolute from the host (``/api/...``)."""

    def __init__(self, base_url, token, verify_tls=True, timeout_s=30.0):
        # type: (str, str, bool, float) -> None
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = "Bearer %s" % token
        self.session.headers["Content-Type"] = "application/json"
        self.verify = verify_tls
        self.timeout = timeout_s

    def request(self, method, path, **kw):
        # type: (str, str, Any) -> requests.Response
        kw.setdefault("verify", self.verify)
        kw.setdefault("timeout", self.timeout)
        return self.session.request(method, self.base + path, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.request("PUT", path, json=json, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    @staticmethod
    def ok(resp, *codes):
        # type: (requests.Response, int) -> Any
        """Assert ``resp.status_code`` is acceptable; return parsed JSON (or None).

        The failure message carries the method, URL and a trimmed body so a broken
        call is diagnosable straight from the pytest report.
        """
        allowed = codes or (200, 201, 204)
        assert resp.status_code in allowed, (
            "%s %s -> %d: %s"
            % (resp.request.method, resp.request.url, resp.status_code, resp.text[:400])
        )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- run helpers ----------------------------------------------------- #

    def launch_run(self, spec_id, body=None):
        # type: (int, Optional[dict]) -> dict
        return self.ok(self.post("/api/specs/%d/run" % spec_id, json=body or {}), 201)

    def wait_for_run(self, run_id, timeout_s, poll_s=3.0):
        # type: (int, float, float) -> dict
        """Poll ``GET /api/runs/{id}`` until the run reaches a terminal state.

        Terminal = completed | stopped | failed. Raises on timeout with the last
        observed state so a stuck run is obvious.
        """
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            last = self.ok(self.get("/api/runs/%d" % run_id))
            if last.get("state") in ("completed", "stopped", "failed"):
                return last
            time.sleep(poll_s)
        raise AssertionError(
            "run %d did not finish within %.0fs; last state=%s"
            % (run_id, timeout_s, (last or {}).get("state"))
        )


class Splunk:
    """Minimal Splunk search client (oneshot) for count assertions.

    Auth is a bearer token when ``token`` is set, else HTTP Basic (user/pass).
    ``verify_tls`` defaults off because lab Splunk usually has a self-signed cert.
    """

    def __init__(self, url, user=None, password=None, token=None, verify_tls=False,
                 timeout_s=60.0):
        # type: (str, Optional[str], Optional[str], Optional[str], bool, float) -> None
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.token = token
        self.verify = verify_tls
        self.timeout = timeout_s

    def _auth_kwargs(self):
        # type: () -> dict
        if self.token:
            return {"headers": {"Authorization": "Bearer %s" % self.token}}
        return {"auth": (self.user or "", self.password or "")}

    def oneshot(self, search, earliest="-1h", latest="now"):
        # type: (str, str, str) -> list
        """Run a blocking oneshot search; return the results rows (list of dicts)."""
        data = {
            "search": search if search.strip().startswith("|") else "search " + search,
            "exec_mode": "oneshot",
            "output_mode": "json",
            "earliest_time": earliest,
            "latest_time": latest,
        }
        resp = requests.post(
            self.url + "/services/search/jobs", data=data,
            verify=self.verify, timeout=self.timeout, **self._auth_kwargs())
        assert resp.status_code == 200, (
            "splunk search -> %d: %s" % (resp.status_code, resp.text[:300]))
        return resp.json().get("results", [])

    def count(self, spl_where, earliest, metric=None, poll_until=0, timeout_s=90.0):
        # type: (str, str, Optional[str], int, float) -> int
        """Count events (or metric measurements) matching ``spl_where``.

        ``spl_where`` is the search body after ``index=`` filters, e.g.
        ``'index=x source="y"'``. For metrics pass ``metric='name'`` -> uses
        ``mstats count(_value)``. When ``poll_until`` > 0 the search is retried
        (indexing lag) until the count reaches it or ``timeout_s`` elapses.
        """
        if metric:
            query = ('| mstats count(_value) as c WHERE %s AND metric_name="%s"'
                     % (spl_where, metric))
        else:
            query = "%s | stats count as c" % spl_where
        deadline = time.time() + timeout_s
        last = 0
        while True:
            rows = self.oneshot(query, earliest=earliest)
            last = int(rows[0]["c"]) if rows and rows[0].get("c") not in (None, "") else 0
            if last >= poll_until or time.time() >= deadline:
                return last
            time.sleep(3)
