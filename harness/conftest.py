"""Pytest fixtures for the Stoker integration harness.

Config comes from the environment (see ``.env.example`` / README). The harness
degrades gracefully: with no ``STOKER_URL`` / ``STOKER_TOKEN`` the whole suite
skips; the live-run tests skip when no HEC target is configured; the Splunk count
assertions skip when Splunk is not configured. So the same files run as a full
end-to-end check locally and as a lighter API-contract check in CI.

Resource fixtures (``make_target``, ``metric_pack``, ``make_spec``) create real
objects over the API and clean them up on teardown. Targets and specs are
deletable; packs are not (no delete endpoint), so ``metric_pack`` is session
scoped and idempotent (reused by name).
"""

from __future__ import annotations

import os
import uuid

import pytest

from clients import Splunk, StokerClient

# Field names whose values are secrets: redacted in repr so a pytest failure
# traceback (which prints fixture values) never leaks a token or password.
_SECRET_FIELDS = frozenset({"token", "hec_token", "splunk_pass", "splunk_token"})


class Config:
    """Harness config. Attribute access like a namespace, but ``repr`` masks
    secrets so they cannot leak into test output / CI logs."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        parts = []
        for key, val in self.__dict__.items():
            shown = "***" if (key in _SECRET_FIELDS and val) else repr(val)
            parts.append("%s=%s" % (key, shown))
        return "Config(%s)" % ", ".join(parts)


def _env(*names, default=None):
    # type: (str, object) -> object
    for name in names:
        val = os.environ.get(name)
        if val not in (None, ""):
            return val
    return default


def _flag(*names, default=False):
    # type: (str, bool) -> bool
    val = _env(*names)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


@pytest.fixture(scope="session")
def cfg():
    # type: () -> SimpleNamespace
    base = _env("STOKER_URL", "STOKER_BASE_URL")
    token = _env("STOKER_TOKEN")
    if not base or not token:
        pytest.skip("set STOKER_URL and STOKER_TOKEN (an operator stk_ token) to run the harness")
    return Config(
        base=base,
        token=token,
        verify_tls=_flag("STOKER_VERIFY_TLS", default=False),
        fleet=_env("STOKER_TEST_FLEET", default="swarm-local"),
        # HEC target the runs deliver to (a lab index you can search + purge).
        hec_url=_env("STOKER_TEST_HEC_URL"),
        hec_token=_env("STOKER_TEST_HEC_TOKEN"),
        hec_verify_tls=_flag("STOKER_TEST_HEC_VERIFY_TLS", default=False),
        event_index=_env("STOKER_TEST_INDEX"),           # events index (eventgen)
        metric_index=_env("STOKER_TEST_METRIC_INDEX"),   # metrics index (metrics)
        eventgen_pack=_env("STOKER_TEST_EVENTGEN_PACK", default="flatline"),
        # Kept deliberately small so storage + search stay cheap.
        eps=float(_env("STOKER_TEST_EPS", default="5")),
        duration_s=int(_env("STOKER_TEST_DURATION_S", default="15")),
        backfill_window_s=int(_env("STOKER_TEST_BACKFILL_WINDOW_S", default="300")),
        backfill_res_s=int(_env("STOKER_TEST_BACKFILL_RES_S", default="30")),
        poll_timeout_s=int(_env("STOKER_TEST_POLL_TIMEOUT_S", default="240")),
        # Splunk (count assertions). Optional: absent -> assert run totals only.
        splunk_url=_env("SPLUNK_URL"),
        splunk_user=_env("SPLUNK_USERNAME", "SPLUNK_USER"),
        splunk_pass=_env("SPLUNK_PASSWORD"),
        splunk_token=_env("SPLUNK_TOKEN"),
    )


@pytest.fixture(scope="session")
def api(cfg):
    # type: (SimpleNamespace) -> StokerClient
    client = StokerClient(cfg.base, cfg.token, verify_tls=cfg.verify_tls)
    probe = client.get("/api/targets")
    if probe.status_code == 401:
        pytest.skip("STOKER_TOKEN was rejected (401) - mint a valid operator token")
    assert probe.status_code == 200, (
        "cannot reach the operator API (/api/targets -> %d: %s)"
        % (probe.status_code, probe.text[:200]))
    return client


@pytest.fixture(scope="session")
def splunk(cfg):
    # type: (SimpleNamespace) -> object
    if not cfg.splunk_url or not (cfg.splunk_token or (cfg.splunk_user and cfg.splunk_pass)):
        return None
    return Splunk(cfg.splunk_url, user=cfg.splunk_user, password=cfg.splunk_pass,
                  token=cfg.splunk_token)


@pytest.fixture
def unique():
    # type: () -> str
    """A short unique tag for isolating a run's data (used as a source override)."""
    return "stoker-harness-" + uuid.uuid4().hex[:10]


@pytest.fixture
def make_target(api, cfg):
    # type: (StokerClient, SimpleNamespace) -> object
    """Factory: create a HEC target pointing at ``index``; delete it on teardown."""
    created = []

    def _make(index, name=None):
        if not cfg.hec_url or not cfg.hec_token:
            pytest.skip("set STOKER_TEST_HEC_URL and STOKER_TEST_HEC_TOKEN for live-run tests")
        body = {
            "name": name or ("harness-tgt-" + uuid.uuid4().hex[:8]),
            "hec_url": cfg.hec_url,
            "token": cfg.hec_token,
            "default_index": index,
            "env_tag": "harness",
            "verify_tls": cfg.hec_verify_tls,
        }
        target = api.ok(api.post("/api/targets", json=body), 201)
        created.append(target["id"])
        return target

    yield _make
    for tid in created:
        api.delete("/api/targets/%d" % tid)


@pytest.fixture(scope="session")
def metric_pack(api):
    # type: (StokerClient) -> dict
    """A small, deterministic metric pack (2 series, 1 metric). Reused by name.

    Packs have no delete endpoint, so this is session scoped and idempotent: it
    refreshes an existing ``stoker-harness-metric`` pack or creates it once.
    """
    name = "stoker-harness-metric"
    config = {
        "resolution_s": 10,
        "seed": 1,
        "dimensions": [{"key": "svc", "values": ["api", "web"]}],  # -> 2 series
        "metrics": [
            {"name": "harness.req.count", "kind": "count", "unit": "requests",
             "min": 1, "p95": 50, "max": 100, "noise": 0.1,
             "pattern": {"type": "sine", "peak_h": 13}},
        ],
    }
    existing = next((p for p in api.ok(api.get("/api/packs")) if p["name"] == name), None)
    if existing:
        pid = existing["id"]
        api.ok(api.put("/api/metric-packs/%d" % pid, json={"name": name, "config": config}))
    else:
        pid = api.ok(api.post("/api/metric-packs", json={"name": name, "config": config}), 201)["id"]
    return api.ok(api.get("/api/metric-packs/%d" % pid))


@pytest.fixture
def make_spec(api, cfg):
    # type: (StokerClient, SimpleNamespace) -> object
    """Factory: create a spec; delete it (and thereby its runs' rows) on teardown."""
    created = []

    def _make(pack_id, target_id, **over):
        body = {
            "name": "harness-spec-" + uuid.uuid4().hex[:8],
            "pack_id": pack_id,
            "target_id": target_id,
            "workers": 1,
            "fleet": cfg.fleet,
        }
        body.update(over)
        spec = api.ok(api.post("/api/specs", json=body), 201)
        created.append(spec["id"])
        return spec

    yield _make
    for sid in created:
        api.delete("/api/specs/%d" % sid)
