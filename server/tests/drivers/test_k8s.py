"""K8sDriver request-shape unit tests + conformance walk (mocked API, no cluster).

Everything here runs against **mocked** ``BatchV1Api`` / ``CoreV1Api`` objects
injected through the driver's constructor -- no real Kubernetes, and (crucially)
**no import of the real ``kubernetes`` client**: the mock and a tiny stand-in
``_ApiException`` (duck-typed on ``.status``) reproduce exactly the surface the
driver touches. This keeps the file hermetic and collectable in the same server
suite that never installs the heavy k8s client.

Two layers, mirroring ``test_conformance.py`` / the SwarmDriver tests:

1. **Request-shape unit tests** (``test_k8s_*``): assert the exact Job/Secret
   manifest the driver builds -- ``completionMode: Indexed``,
   ``parallelism == completions == N``, ``backoffLimit == 3*N``,
   ``ttlSecondsAfterFinished``, ``restartPolicy: OnFailure``,
   ``automountServiceAccountToken: false``, labels ``stoker.run=<id>``, the
   worker image, the ``STOKER_*`` env -- and prove the HEC token is delivered via
   a Secret + ``secretKeyRef`` and **never** as a plaintext pod-spec env value
   (the built manifest object is grepped). Also: the per-run Secret's
   ``ownerReference`` to the Job, ``scale`` patching parallelism AND completions
   together, idempotent 404 ``destroy``, ``status`` mapping + its transient-vs-404
   semantics.
2. **Conformance walk** (``test_k8s_conformance_*``): the shared six-method state
   machine (create -> status(N) -> scale -> stop -> destroy, idempotent) against a
   stateful in-memory fake of the two APIs, so the K8sDriver code paths run in CI
   without a cluster -- the k8s analogue of the swarm-mock leg.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from server.drivers.base import (
    DriverError,
    DriverRef,
    DriverStatus,
    ExecutionDriver,
    NotFound,
    RunSnapshot,
)
from server.drivers.k8s import K8sDriver, job_name, secret_name


# --------------------------------------------------------------------------- #
# Test doubles: a kubernetes-style ApiException and stateful fake APIs.
# --------------------------------------------------------------------------- #

class _ApiException(Exception):
    """Stand-in for ``kubernetes.client.rest.ApiException``.

    The real client raises this with an integer ``.status``; the driver's error
    mapping keys off that attribute (duck-typed), so this reproduces the surface
    without importing the multi-megabyte client. Constructed with ``status=404``
    for "gone", ``status=500`` for a transient failure.
    """

    def __init__(self, status, reason="", body=""):
        # type: (int, str, str) -> None
        super(_ApiException, self).__init__("(%s) %s" % (status, reason))
        self.status = status
        self.reason = reason
        self.body = body


def _snapshot(run_id=812, workers=4, with_hec=True, driver_opts=None):
    # type: (int, int, bool, Optional[Dict[str, Any]]) -> RunSnapshot
    """A RunSnapshot mirroring what lifecycle.build_run_snapshot projects."""
    env = {
        "STOKER_RUN_ID": str(run_id),
        "STOKER_CONTROL_URL": "https://stoker.test",
        "STOKER_RUN_JWT": "jwt.header.payload.sig",
        "STOKER_TOTAL_WORKERS": str(workers),
    }
    if with_hec:
        env["STOKER_HEC_TOKEN"] = "super-secret-hec-token"
    return RunSnapshot(
        run_id=run_id,
        image="ghcr.io/livehybrid/stoker-worker@sha256:deadbeef",
        env=env,
        labels={"stoker.run": str(run_id)},
        driver_opts=driver_opts or {},
        stop_grace_s=45,
    )


def _mock_apis(job_uid="job-uid-abc"):
    # type: (str) -> Any
    """A pair of ``unittest.mock`` APIs with sensible return values.

    ``create_namespaced_job`` returns an object whose ``.metadata.uid`` is set
    (the driver reads it to build the Secret's ownerReference). Everything else
    is a bare Mock the tests introspect via ``call_args``.
    """
    batch = mock.Mock(name="BatchV1Api")
    core = mock.Mock(name="CoreV1Api")
    created_job = mock.Mock(name="V1Job")
    created_job.metadata = mock.Mock(uid=job_uid)
    batch.create_namespaced_job.return_value = created_job
    return batch, core


def _driver(batch, core, namespace="stoker"):
    # type: (Any, Any, str) -> K8sDriver
    return K8sDriver(namespace=namespace, batch_api=batch, core_api=core)


# --------------------------------------------------------------------------- #
# Manifest helpers: pull the body= kwarg the driver passed to the mocked API.
# --------------------------------------------------------------------------- #

def _created_job_body(batch):
    # type: (Any) -> Dict[str, Any]
    """The Job manifest dict the driver handed to create_namespaced_job."""
    assert batch.create_namespaced_job.called, "Job was never created"
    return batch.create_namespaced_job.call_args.kwargs["body"]


def _created_secret_body(core):
    # type: (Any) -> Dict[str, Any]
    """The Secret manifest dict the driver handed to create_namespaced_secret."""
    assert core.create_namespaced_secret.called, "Secret was never created"
    return core.create_namespaced_secret.call_args.kwargs["body"]


def _pod_env(job_body):
    # type: (Dict[str, Any]) -> List[Dict[str, Any]]
    return job_body["spec"]["template"]["spec"]["containers"][0]["env"]


def _pod_spec(job_body):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    return job_body["spec"]["template"]["spec"]


# =========================================================================== #
# 1. create(): the Job manifest shape.
# =========================================================================== #

def test_k8s_create_builds_indexed_job_with_exact_shape():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    snap = _snapshot(run_id=812, workers=4)

    ref = driver.create(snap, 4)

    # DriverRef contract (mirrors SwarmDriver: kind + native id + raw addressing).
    assert ref.kind == "k8s"
    assert ref.id == "stoker-run-812"
    assert ref.raw["run_id"] == 812
    assert ref.raw["name"] == "stoker-run-812"
    assert ref.raw["namespace"] == "stoker"

    # The Job was created in the right namespace.
    assert batch.create_namespaced_job.call_args.kwargs["namespace"] == "stoker"

    job = _created_job_body(batch)
    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"]["name"] == "stoker-run-812"
    assert job["metadata"]["labels"]["stoker.run"] == "812"

    spec = job["spec"]
    # completionMode Indexed; parallelism == completions == N; backoffLimit 3*N.
    assert spec["completionMode"] == "Indexed"
    assert spec["parallelism"] == 4
    assert spec["completions"] == 4
    assert spec["backoffLimit"] == 3 * 4
    assert spec["ttlSecondsAfterFinished"] == 3600

    pod = _pod_spec(job)
    assert pod["restartPolicy"] == "OnFailure"
    # Workers never talk to the API server -> no SA token mounted.
    assert pod["automountServiceAccountToken"] is False

    template_labels = job["spec"]["template"]["metadata"]["labels"]
    assert template_labels["stoker.run"] == "812"

    container = pod["containers"][0]
    assert container["image"] == "ghcr.io/livehybrid/stoker-worker@sha256:deadbeef"


def test_k8s_create_env_carries_run_identity():
    batch, core = _mock_apis()
    driver = _driver(batch, core)

    driver.create(_snapshot(run_id=812, workers=4), 4)

    env = _pod_env(_created_job_body(batch))
    plain = {e["name"]: e.get("value") for e in env if "value" in e}
    assert plain["STOKER_RUN_ID"] == "812"
    assert plain["STOKER_CONTROL_URL"] == "https://stoker.test"
    assert plain["STOKER_TOTAL_WORKERS"] == "4"
    assert plain["STOKER_RUN_JWT"] == "jwt.header.payload.sig"


def test_k8s_create_backoff_limit_tracks_worker_count():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    driver.create(_snapshot(run_id=7, workers=6), 6)
    spec = _created_job_body(batch)["spec"]
    assert spec["parallelism"] == 6
    assert spec["completions"] == 6
    assert spec["backoffLimit"] == 18  # 3 * 6


def test_k8s_create_bounded_run_sets_active_deadline():
    """A bounded run (duration in driver_opts) gets activeDeadlineSeconds=dur+300."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    snap = _snapshot(driver_opts={"duration_s": 14400})

    driver.create(snap, 4)

    spec = _created_job_body(batch)["spec"]
    assert spec["activeDeadlineSeconds"] == 14400 + 300


def test_k8s_create_unbounded_run_has_no_active_deadline():
    batch, core = _mock_apis()
    driver = _driver(batch, core)

    driver.create(_snapshot(driver_opts={}), 4)  # no duration -> unbounded

    spec = _created_job_body(batch)["spec"]
    assert "activeDeadlineSeconds" not in spec


def test_k8s_create_passes_completion_index_as_hint_slot():
    """JOB_COMPLETION_INDEX is surfaced to the worker as STOKER_HINT_SLOT."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)

    driver.create(_snapshot(), 4)

    env = _pod_env(_created_job_body(batch))
    hint = [e for e in env if e["name"] == "STOKER_HINT_SLOT"]
    assert len(hint) == 1
    # It must reference the completion index (downward API), not be a static value.
    assert "value" not in hint[0]
    field = hint[0]["valueFrom"]["fieldRef"]["fieldPath"]
    assert "job-completion-index" in field


def test_k8s_create_rejects_zero_workers_before_any_call():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    with pytest.raises(DriverError):
        driver.create(_snapshot(workers=1), 0)
    batch.create_namespaced_job.assert_not_called()
    core.create_namespaced_secret.assert_not_called()


# =========================================================================== #
# 2. The HEC token: Secret + secretKeyRef, NEVER a plaintext pod-spec env value.
# =========================================================================== #

def test_k8s_hec_token_is_delivered_via_secret_key_ref():
    batch, core = _mock_apis()
    driver = _driver(batch, core)

    driver.create(_snapshot(run_id=812), 4)

    env = _pod_env(_created_job_body(batch))
    hec = [e for e in env if e["name"] == "STOKER_HEC_TOKEN"]
    assert len(hec) == 1, "STOKER_HEC_TOKEN must be present exactly once"
    entry = hec[0]
    # Delivered by reference, not value.
    assert "value" not in entry
    ref = entry["valueFrom"]["secretKeyRef"]
    assert ref["name"] == secret_name(812)   # the per-run Secret
    assert ref["key"]                          # some key inside it


def test_k8s_hec_token_never_appears_as_plaintext_on_pod_spec():
    """Grep the entire built Job manifest: the raw token must not be in the pod spec."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    snap = _snapshot(run_id=812)
    token = snap.env["STOKER_HEC_TOKEN"]

    driver.create(snap, 4)

    job = _created_job_body(batch)
    # Serialise the whole Job object and assert the secret value is nowhere in it.
    blob = json.dumps(job)
    assert token not in blob, "HEC token leaked into the Job/pod-spec manifest"

    # And no env entry carries the token as an inline "value".
    for e in _pod_env(job):
        assert e.get("value") != token


def test_k8s_hec_token_is_in_the_secret_data_not_the_job_env():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    snap = _snapshot(run_id=812)
    token = snap.env["STOKER_HEC_TOKEN"]

    driver.create(snap, 4)

    # The token lives in the Secret payload (stringData or base64 data), addressed
    # by the same key the pod's secretKeyRef names.
    secret = _created_secret_body(core)
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == secret_name(812)

    key = _pod_env_secret_key(_created_job_body(batch))
    payload = _secret_value_for_key(secret, key)
    assert payload is not None, "the secretKeyRef key is not present in the Secret"
    # Whether via stringData (plain) or data (base64), it must resolve to the token.
    assert _decode_secret_value(payload) == token


def test_k8s_no_hec_token_creates_no_secret_and_no_hec_env():
    """A target with no token: no Secret, no STOKER_HEC_TOKEN env at all."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)

    driver.create(_snapshot(with_hec=False), 4)

    core.create_namespaced_secret.assert_not_called()
    env = _pod_env(_created_job_body(batch))
    assert not [e for e in env if e["name"] == "STOKER_HEC_TOKEN"]


# =========================================================================== #
# 3. The per-run Secret carries an ownerReference to the Job (garbage collection).
# =========================================================================== #

def test_k8s_secret_gets_owner_reference_to_the_job():
    batch, core = _mock_apis(job_uid="job-uid-812")
    driver = _driver(batch, core)

    driver.create(_snapshot(run_id=812), 4)

    owner = _secret_owner_reference(core)
    assert owner is not None, "the per-run Secret has no ownerReference to the Job"
    assert owner["kind"] == "Job"
    assert owner["name"] == job_name(812)
    assert owner["uid"] == "job-uid-812"     # the created Job's uid (for GC)
    # A controller/GC owner reference so the Secret is reaped with the Job.
    assert owner.get("controller") is True


def test_k8s_secret_owner_reference_uses_the_created_jobs_uid():
    """The ownerReference uid comes from the Job the API actually returned."""
    batch, core = _mock_apis(job_uid="the-real-uid-999")
    driver = _driver(batch, core)

    driver.create(_snapshot(run_id=5), 2)

    owner = _secret_owner_reference(core)
    assert owner["uid"] == "the-real-uid-999"


# =========================================================================== #
# 4. scale(): patch parallelism AND completions together (Elastic Indexed Jobs).
# =========================================================================== #

def test_k8s_scale_patches_parallelism_and_completions_together():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(run_id=812), 3)

    batch.reset_mock()
    driver.scale(ref, 7)

    assert batch.patch_namespaced_job.called
    call = batch.patch_namespaced_job.call_args
    assert call.kwargs["name"] == "stoker-run-812"
    assert call.kwargs["namespace"] == "stoker"
    patched = call.kwargs["body"]["spec"]
    # BOTH must move together, else the Indexed Job never completes / rejects.
    assert patched["parallelism"] == 7
    assert patched["completions"] == 7


def test_k8s_scale_rejects_negative():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(), 3)
    batch.reset_mock()
    with pytest.raises(DriverError):
        driver.scale(ref, -1)
    batch.patch_namespaced_job.assert_not_called()


# =========================================================================== #
# 5. destroy(): delete the Job, idempotent (a 404 from the API is success).
# =========================================================================== #

def test_k8s_destroy_deletes_the_job():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(run_id=812), 3)

    batch.reset_mock()
    driver.destroy(ref)

    assert batch.delete_namespaced_job.called
    call = batch.delete_namespaced_job.call_args
    assert call.kwargs["name"] == "stoker-run-812"
    assert call.kwargs["namespace"] == "stoker"


def test_k8s_destroy_is_idempotent_on_404():
    """A 404 (Job already gone) is surfaced as NotFound inside the driver and
    swallowed: destroy must not raise."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(), 2)

    batch.delete_namespaced_job.side_effect = _ApiException(status=404, reason="Not Found")
    # Must not raise -- the fleet is already gone, which is success.
    driver.destroy(ref)
    assert batch.delete_namespaced_job.called


def test_k8s_destroy_propagates_transient_error():
    """A non-404 API error on destroy is a real failure, not idempotent success."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(), 2)

    batch.delete_namespaced_job.side_effect = _ApiException(status=500, reason="boom")
    with pytest.raises(DriverError):
        driver.destroy(ref)


# =========================================================================== #
# 6. status(): map Job + pods to DriverStatus; transient(500) raises, 404 -> 0.
# =========================================================================== #

def test_k8s_status_maps_job_and_pods_to_desired_and_running():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(run_id=812, workers=3), 3)

    # Job reports parallelism 3; three pods, two Running one Pending.
    batch.read_namespaced_job.return_value = _job_obj(parallelism=3, active=3)
    core.list_namespaced_pod.return_value = _pod_list([
        _pod("stoker-run-812-0", "Running", node="n1", index=0),
        _pod("stoker-run-812-1", "Running", node="n2", index=1),
        _pod("stoker-run-812-2", "Pending", node=None, index=2),
    ])

    status = driver.status(ref)
    assert isinstance(status, DriverStatus)
    assert status.desired == 3
    assert status.running == 2                # only the Running pods count
    assert len(status.tasks) == 3
    for task in status.tasks:
        assert set(task.keys()) >= {"slot", "holder", "node", "state"}
    # The pod list was filtered by the run label.
    selector = core.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert selector == "stoker.run=812"


def test_k8s_status_counts_only_running_pods():
    """Succeeded/Failed pods (kept until ttl) must not inflate `running`."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(run_id=812, workers=2), 2)

    batch.read_namespaced_job.return_value = _job_obj(parallelism=2, active=1)
    core.list_namespaced_pod.return_value = _pod_list([
        _pod("stoker-run-812-0", "Running", node="n1", index=0),
        _pod("stoker-run-812-1", "Succeeded", node="n2", index=1),
    ])

    status = driver.status(ref)
    assert status.desired == 2
    assert status.running == 1
    states = {t["state"] for t in status.tasks}
    assert "Succeeded" in states               # still surfaced for observability


def test_k8s_status_returns_zero_desired_only_on_a_real_404():
    """A genuine 404 (Job destroyed) -> desired 0, matching the FakeDriver."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(run_id=812), 3)

    batch.read_namespaced_job.side_effect = _ApiException(status=404, reason="Not Found")
    status = driver.status(ref)
    assert status.desired == 0
    assert status.running == 0


def test_k8s_status_raises_on_transient_500_never_reports_zero():
    """A transient 500 must PROPAGATE (unknown), never be coerced to desired=0.

    This is the boot-reconciliation safety property: a hiccup must not be mistaken
    for a destroyed fleet (which would orphan a live run)."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(run_id=812), 3)

    batch.read_namespaced_job.side_effect = _ApiException(status=500, reason="server error")
    with pytest.raises(DriverError):
        driver.status(ref)


def test_k8s_status_transient_error_is_not_notfound():
    """The 500 path raises DriverError but NOT NotFound (callers branch on it)."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    ref = driver.create(_snapshot(), 3)

    batch.read_namespaced_job.side_effect = _ApiException(status=503, reason="unavailable")
    with pytest.raises(DriverError) as exc:
        driver.status(ref)
    assert not isinstance(exc.value, NotFound)


# =========================================================================== #
# 7. No secret leaks into raised error messages.
# =========================================================================== #

def test_k8s_error_message_does_not_leak_the_hec_token():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    snap = _snapshot(run_id=812)
    token = snap.env["STOKER_HEC_TOKEN"]

    # Fail the Job create with a transient error; the raised DriverError must be
    # actionable but never carry the secret.
    batch.create_namespaced_job.side_effect = _ApiException(status=500, reason="boom")
    with pytest.raises(DriverError) as exc:
        driver.create(snap, 3)
    assert token not in str(exc.value)


# =========================================================================== #
# 8. The driver satisfies the ExecutionDriver Protocol (runtime_checkable).
# =========================================================================== #

def test_k8s_driver_satisfies_execution_driver_protocol():
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    assert isinstance(driver, ExecutionDriver)


def test_k8s_driver_exposes_all_six_methods():
    driver = _driver(*_mock_apis())
    for name in ("create", "scale", "stop", "destroy", "status", "logs"):
        assert callable(getattr(driver, name))


def test_job_and_secret_name_scheme():
    assert job_name(812) == "stoker-run-812"
    assert job_name("abc") == "stoker-run-abc"
    # The per-run Secret is derived from the run id and distinct from the Job name.
    assert secret_name(812) != job_name(812)
    assert "812" in secret_name(812)


# --------------------------------------------------------------------------- #
# Secret-manifest introspection helpers (tolerate stringData or base64 data).
# --------------------------------------------------------------------------- #

def _pod_env_secret_key(job_body):
    # type: (Dict[str, Any]) -> str
    for e in _pod_env(job_body):
        if e["name"] == "STOKER_HEC_TOKEN" and "valueFrom" in e:
            return e["valueFrom"]["secretKeyRef"]["key"]
    raise AssertionError("no STOKER_HEC_TOKEN secretKeyRef in the pod env")


def _secret_value_for_key(secret, key):
    # type: (Dict[str, Any], str) -> Optional[str]
    if key in (secret.get("stringData") or {}):
        return secret["stringData"][key]
    if key in (secret.get("data") or {}):
        return secret["data"][key]
    return None


def _decode_secret_value(value):
    # type: (str) -> str
    """Return the plaintext for a Secret value (base64 in ``data``, plain in
    ``stringData``). Tries base64 first, falls back to the raw string."""
    import base64
    import binascii
    try:
        decoded = base64.b64decode(value, validate=True)
        # Only treat it as base64 if it round-trips to valid text and the input
        # actually looked encoded (stringData plaintext rarely does).
        text = decoded.decode("utf-8")
        # A plaintext token that happens to be valid base64 would decode to
        # bytes != the token; guard by re-encoding.
        if base64.b64encode(decoded).decode("ascii") == value:
            return text
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    return value


def _secret_owner_reference(core):
    # type: (Any) -> Optional[Dict[str, Any]]
    """Return the ownerReference the driver put on the per-run Secret.

    Accepts either shape: the Secret manifest carried it at create time, or the
    driver patched it on afterwards (needing the Job uid). Prefers the patch.
    """
    if core.patch_namespaced_secret.called:
        body = core.patch_namespaced_secret.call_args.kwargs["body"]
        owners = (body.get("metadata") or {}).get("ownerReferences") or []
        if owners:
            return owners[0]
    if core.create_namespaced_secret.called:
        body = core.create_namespaced_secret.call_args.kwargs["body"]
        owners = (body.get("metadata") or {}).get("ownerReferences") or []
        if owners:
            return owners[0]
    return None


# --------------------------------------------------------------------------- #
# k8s object builders for status() (dict-shaped; the driver reads both dicts and
# real model objects, so dicts are sufficient and hermetic).
# --------------------------------------------------------------------------- #

def _job_obj(parallelism, active=0, uid="job-uid"):
    # type: (int, int, str) -> Dict[str, Any]
    return {
        "metadata": {"name": "stoker-run-812", "uid": uid},
        "spec": {"parallelism": parallelism, "completions": parallelism},
        "status": {"active": active},
    }


def _pod(name, phase, node=None, index=None):
    # type: (str, str, Optional[str], Optional[int]) -> Dict[str, Any]
    annotations = {}
    if index is not None:
        annotations["batch.kubernetes.io/job-completion-index"] = str(index)
    return {
        "metadata": {"name": name, "annotations": annotations},
        "spec": {"node_name": node, "nodeName": node},
        "status": {"phase": phase},
    }


def _pod_list(pods):
    # type: (List[Dict[str, Any]]) -> Dict[str, Any]
    return {"items": pods}


# =========================================================================== #
# Conformance walk: create -> status(N) -> scale -> stop -> destroy (idempotent),
# against a stateful in-memory fake of the two APIs (no cluster). This is the
# k8s analogue of test_conformance.py's swarm-mock leg.
# =========================================================================== #

class _FakeK8s:
    """A stateful fake of the BatchV1Api/CoreV1Api subset the driver uses.

    Stores one Job keyed by name (parallelism/completions/uid) and synthesises one
    Running pod per parallelism when pods are listed, so a full create -> status ->
    scale -> stop -> destroy walk behaves like a tiny cluster. A deleted Job 404s
    on subsequent read/delete (via the driver's NotFound mapping)."""

    def __init__(self):
        # type: () -> None
        self.jobs = {}      # type: Dict[str, Dict[str, Any]]
        self.secrets = {}   # type: Dict[str, Dict[str, Any]]
        self._uid = 1000

    # BatchV1Api ---------------------------------------------------------- #

    def create_namespaced_job(self, namespace, body, **kw):
        # type: (str, Dict[str, Any], Any) -> Any
        name = body["metadata"]["name"]
        self._uid += 1
        uid = "uid-%d" % self._uid
        # Preserve the stoker.run label the driver stamps on create, so the
        # list_run_ids enumeration parses it from the label (its primary path)
        # rather than only the name fallback.
        labels = dict(body["metadata"].get("labels") or {})
        self.jobs[name] = {
            "metadata": {"name": name, "uid": uid, "labels": labels},
            "spec": {
                "parallelism": body["spec"]["parallelism"],
                "completions": body["spec"]["completions"],
            },
            "status": {"active": body["spec"]["parallelism"]},
        }
        return _obj(self.jobs[name])

    def read_namespaced_job(self, name, namespace, **kw):
        # type: (str, str, Any) -> Any
        job = self.jobs.get(name)
        if job is None:
            raise _ApiException(status=404, reason="Not Found")
        return _obj(job)

    def patch_namespaced_job(self, name, namespace, body, **kw):
        # type: (str, str, Dict[str, Any], Any) -> Any
        job = self.jobs.get(name)
        if job is None:
            raise _ApiException(status=404, reason="Not Found")
        spec = body.get("spec", {})
        if "parallelism" in spec:
            job["spec"]["parallelism"] = spec["parallelism"]
            job["status"]["active"] = spec["parallelism"]
        if "completions" in spec:
            job["spec"]["completions"] = spec["completions"]
        return _obj(job)

    def delete_namespaced_job(self, name, namespace, **kw):
        # type: (str, str, Any) -> Any
        if name not in self.jobs:
            raise _ApiException(status=404, reason="Not Found")
        del self.jobs[name]
        return _obj({"status": "Success"})

    def list_namespaced_job(self, namespace, label_selector=None, **kw):
        # type: (str, Optional[str], Any) -> Any
        # Presence-only selector "stoker.run": every job stored here carries the
        # label (the driver stamps it on create), so return them all.
        return _obj({"items": [_obj(job) for job in self.jobs.values()]})

    # CoreV1Api ----------------------------------------------------------- #

    def create_namespaced_secret(self, namespace, body, **kw):
        # type: (str, Dict[str, Any], Any) -> Any
        name = body["metadata"]["name"]
        self.secrets[name] = body
        return _obj(body)

    def patch_namespaced_secret(self, name, namespace, body, **kw):
        # type: (str, str, Dict[str, Any], Any) -> Any
        self.secrets.setdefault(name, {"metadata": {"name": name}})
        return _obj(self.secrets[name])

    def list_namespaced_pod(self, namespace, label_selector=None, **kw):
        # type: (str, Optional[str], Any) -> Any
        # Derive the run id from the selector; return one Running pod per replica.
        run = (label_selector or "").split("=", 1)[-1]
        name = "stoker-run-%s" % run
        job = self.jobs.get(name)
        pods = []  # type: List[Dict[str, Any]]
        if job is not None:
            for i in range(int(job["spec"]["parallelism"])):
                pods.append(_pod("%s-%d" % (name, i), "Running", node="n%d" % (i % 3), index=i))
        return _obj({"items": pods})

    def read_namespaced_pod_log(self, name, namespace, tail_lines=None, **kw):
        # type: (str, str, Optional[int], Any) -> str
        return "log line from %s\n" % name


def _obj(d):
    # type: (Dict[str, Any]) -> Any
    """Wrap a dict as an attribute-accessible object (like a k8s model).

    The driver's ``_attr`` reads both dicts and objects; returning objects here
    exercises the attribute-access path (the real client's shape) while the
    request-shape tests above exercise the dict path."""
    return _Bunch(d)


# Fields that the real kubernetes client keeps as plain ``Dict[str, str]`` on the
# model object (``V1ObjectMeta.annotations`` / ``.labels``) rather than wrapping
# in a sub-object. The driver reads annotations as a dict, so the fake must too.
_DICT_MAP_KEYS = frozenset({"annotations", "labels"})


class _Bunch:
    """Recursive attribute view over a dict (nested dicts/lists wrapped too).

    Faithful to the real k8s client: structured fields become sub-objects, but
    string-map fields (annotations, labels) stay plain dicts."""

    def __init__(self, d):
        # type: (Dict[str, Any]) -> None
        for key, value in d.items():
            setattr(self, key, value if key in _DICT_MAP_KEYS else _wrap(value))


def _wrap(value):
    # type: (Any) -> Any
    if isinstance(value, dict):
        return _Bunch(value)
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


@pytest.fixture()
def k8s_conformance_driver():
    # type: () -> K8sDriver
    fake = _FakeK8s()
    return K8sDriver(namespace="stoker", batch_api=fake, core_api=fake)


def test_k8s_conformance_lifecycle(k8s_conformance_driver):
    """The K8sDriver honours the six-method state machine (create->...->destroy)."""
    driver = k8s_conformance_driver
    snap = _snapshot(run_id=812, workers=3)

    ref = driver.create(snap, 3)
    try:
        assert isinstance(ref, DriverRef)
        assert ref.id == "stoker-run-812"

        status = driver.status(ref)
        assert isinstance(status, DriverStatus)
        assert status.desired == 3
        assert status.running == 3

        driver.scale(ref, 5)
        assert driver.status(ref).desired == 5

        driver.scale(ref, 2)
        assert driver.status(ref).desired == 2

        driver.stop(ref, grace_s=45)
        assert driver.status(ref).desired == 0
    finally:
        driver.destroy(ref)

    # After destroy the fleet reports gone, and destroy is idempotent.
    assert driver.status(ref).desired == 0
    driver.destroy(ref)  # must not raise
    assert driver.status(ref).desired == 0


def test_k8s_conformance_create_rejects_zero_workers(k8s_conformance_driver):
    with pytest.raises(DriverError):
        k8s_conformance_driver.create(_snapshot(workers=1), 0)


def test_k8s_conformance_status_after_create_reports_tasks(k8s_conformance_driver):
    driver = k8s_conformance_driver
    ref = driver.create(_snapshot(run_id=813, workers=2), 2)
    try:
        status = driver.status(ref)
        assert status.desired == 2
        assert isinstance(status.tasks, list)
        for task in status.tasks:
            assert set(task.keys()) >= {"slot", "holder", "node", "state"}
    finally:
        driver.destroy(ref)


def test_k8s_conformance_logs_returns_pod_output(k8s_conformance_driver):
    driver = k8s_conformance_driver
    ref = driver.create(_snapshot(run_id=814, workers=2), 2)
    try:
        text = driver.logs(ref, slot=None, tail=50)
        assert "log line from stoker-run-814-0" in text
        # A specific slot narrows to that pod.
        one = driver.logs(ref, slot=1, tail=50)
        assert "stoker-run-814-1" in one
        assert "stoker-run-814-0" not in one
    finally:
        driver.destroy(ref)


# =========================================================================== #
# list_run_ids(): the optional 7th (discovery-only) method for the boot sweep.
# =========================================================================== #

def test_k8s_conformance_list_run_ids(k8s_conformance_driver):
    """The fake-cluster driver reports owned run ids and drops destroyed ones."""
    driver = k8s_conformance_driver
    r1 = driver.create(_snapshot(run_id=815, workers=2), 2)
    driver.create(_snapshot(run_id=816, workers=1), 1)
    owned = driver.list_run_ids()
    assert isinstance(owned, set)
    assert owned == {815, 816}
    driver.destroy(r1)
    assert driver.list_run_ids() == {816}


def test_k8s_list_run_ids_lists_jobs_by_label_and_parses_id():
    """list_run_ids lists Jobs with the stoker.run selector and parses the id."""
    batch, core = _mock_apis()
    driver = _driver(batch, core)
    batch.list_namespaced_job.return_value = _obj({"items": [
        _obj({"metadata": {"name": "stoker-run-3", "labels": {"stoker.run": "3"}}}),
        _obj({"metadata": {"name": "stoker-run-77", "labels": {"stoker.run": "77"}}}),
        # No label -> parsed from the stoker-run-<id> name fallback.
        _obj({"metadata": {"name": "stoker-run-9"}}),
        # Unparseable id -> skipped, never guessed.
        _obj({"metadata": {"name": "weird", "labels": {"stoker.run": "nope"}}}),
    ]})

    owned = driver.list_run_ids()
    assert owned == {3, 77, 9}

    kwargs = batch.list_namespaced_job.call_args.kwargs
    assert kwargs["namespace"] == "stoker"
    assert kwargs["label_selector"] == "stoker.run"


def test_k8s_list_run_ids_raises_on_backend_error_not_empty():
    """A 5xx during enumeration raises DriverError (never silent empty)."""
    batch, core = _mock_apis()
    batch.list_namespaced_job.side_effect = _ApiException(status=500, reason="boom")
    driver = _driver(batch, core)
    with pytest.raises(DriverError):
        driver.list_run_ids()


def test_k8s_orphaned_secret_deleted_when_job_create_fails():
    # Regression (review low): if the Job create fails after the per-run Secret
    # was created, the Secret (whose only GC owner would have been the Job) must
    # be deleted so no HEC credential lingers in the namespace.
    batch, core = _mock_apis()
    batch.create_namespaced_job.side_effect = _ApiException(status=500, reason="boom")
    driver = _driver(batch, core)

    with pytest.raises(DriverError):
        driver.create(_snapshot(run_id=812, with_hec=True), 3)

    assert core.create_namespaced_secret.called, "secret is created before the job"
    assert core.delete_namespaced_secret.called, "orphaned secret must be cleaned up"
    assert core.delete_namespaced_secret.call_args.kwargs["name"] == secret_name(812)


def test_k8s_no_secret_no_cleanup_when_job_create_fails():
    # No HEC token -> no Secret was created, so nothing to clean up.
    batch, core = _mock_apis()
    batch.create_namespaced_job.side_effect = _ApiException(status=500, reason="boom")
    driver = _driver(batch, core)

    with pytest.raises(DriverError):
        driver.create(_snapshot(run_id=813, with_hec=False), 2)

    core.create_namespaced_secret.assert_not_called()
    core.delete_namespaced_secret.assert_not_called()
