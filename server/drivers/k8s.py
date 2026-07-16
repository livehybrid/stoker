"""K8sDriver: an :class:`ExecutionDriver` backed by a Kubernetes ``batch/v1`` Job.

One run == one Indexed Job named ``stoker-run-<id>`` labelled ``stoker.run=<id>``.
The control plane still owns worker identity (the lease); ``JOB_COMPLETION_INDEX``
is passed to the worker as ``STOKER_HINT_SLOT`` (a hint honoured only when the
matching lease is free), so ``status`` pod mapping is best-effort observability.

Design ground truth (AIOS ``DESIGN.md`` sections 5 + 11 "AWS"):

* ``create``  = one ``batch/v1`` Job, ``completionMode: Indexed``,
  ``parallelism == completions == N``, ``backoffLimit: 3*N``,
  ``ttlSecondsAfterFinished: 3600``, ``activeDeadlineSeconds = duration + 300``
  for bounded runs, ``restartPolicy: OnFailure``. The HEC token rides in a
  per-run ephemeral Secret with an ``ownerReference`` on the Job (garbage-
  collected with it) and is projected into the pod via ``secretKeyRef`` -- never
  as a plaintext env value. Pods run ``automountServiceAccountToken: false`` and
  set ``imagePullPolicy: Always`` so a floating worker tag is never served stale
  from a node's cache (an already-digest-pinned ``run.image`` is immutable
  regardless).
* ``scale``   = Elastic Indexed Jobs (k8s >= 1.27): patch ``parallelism`` **and**
  ``completions`` together.
* ``stop``    = delete the Job with ``propagationPolicy: Foreground`` (pods get
  SIGTERM and drain within ``terminationGracePeriodSeconds``; the control plane
  also answers ``drain`` on heartbeats). The Secret GCs via its ownerRef.
* ``destroy`` = delete the Job with ``propagationPolicy: Foreground`` (the Secret
  and pods GC with it). Idempotent: a genuine 404 == already gone.
* ``status``  = read the Job (desired = ``spec.parallelism``) + list pods by
  ``stoker.run=<id>`` folded into a :class:`DriverStatus`.
* ``logs``    = read pod logs (whole Job when ``slot`` is None, else the pod whose
  completion index matches ``slot``), tailed.

One driver serves k3s (local) and EKS (AWS) via different kubeconfig contexts;
the API objects (``BatchV1Api`` / ``CoreV1Api``) are injectable so the request
shapes are unit-tested against a mock with no real cluster. A genuine 404 (Job
gone) is distinct from a transient failure (timeout / 5xx): callers must not
coerce a hiccup into "absent". No secret (HEC token or JWT) is ever logged, and
the token never appears in the Job's pod-spec env (only in the Secret).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from .base import DriverError, DriverRef, DriverStatus, NotFound, RunSnapshot
from ..config import get_settings

log = logging.getLogger("stoker.driver.k8s")

_KIND = "k8s"

# The env var Kubernetes sets on each Indexed-Job pod; the worker reads it as its
# slot hint (WORKER-CONTRACT: STOKER_HINT_SLOT <- JOB_COMPLETION_INDEX).
_COMPLETION_INDEX_ENV = "JOB_COMPLETION_INDEX"

# Pod phases: only "Running" counts a pod as up. Pending/Unknown are coming up;
# Succeeded/Failed are terminal (an Indexed Job keeps completed pods around until
# ttlSecondsAfterFinished, so they must not inflate "running").
_RUNNING_PHASES = frozenset({"Running"})

# The Secret key under which the HEC token is stored (and referenced by the pod
# via secretKeyRef). The env var the worker actually reads is STOKER_HEC_TOKEN.
_HEC_ENV = "STOKER_HEC_TOKEN"
_HEC_SECRET_KEY = "hec-token"


def job_name(run_id):
    # type: (Any) -> str
    """The Job name for a run (single source of the naming scheme)."""
    return "stoker-run-%s" % run_id


def secret_name(run_id):
    # type: (Any) -> str
    """The per-run ephemeral Secret name (carries the HEC token)."""
    return "stoker-run-%s-hec" % run_id


class K8sDriver(object):
    """Kubernetes-backed execution driver (one Indexed ``batch/v1`` Job per run)."""

    def __init__(self, namespace="stoker", batch_api=None, core_api=None,
                 context=None):
        # type: (str, Optional[Any], Optional[Any], Optional[str]) -> None
        """
        Args:
            namespace: the namespace all Jobs/Secrets/pods live in.
            batch_api: a ``kubernetes.client.BatchV1Api`` (injected for tests).
            core_api: a ``kubernetes.client.CoreV1Api`` (injected for tests).
            context: kubeconfig context name (k3s vs EKS); used only when the
                driver builds its own clients (``from_fleet_config``).
        """
        self._namespace = namespace or "stoker"
        self._context = context
        # Injectable for unit tests (mocks); built lazily from kubeconfig when
        # absent so a long-lived driver picks up the configured context.
        self._batch = batch_api
        self._core = core_api

    @classmethod
    def from_fleet_config(cls, config):
        # type: (Optional[Dict[str, Any]]) -> "K8sDriver"
        """Build from a ``fleets.config_json`` (kubeconfig context + namespace).

        The kubeconfig context selects k3s (local) or EKS (AWS); the namespace
        defaults to ``stoker``. The kubernetes client is imported lazily so the
        control plane never pulls it in unless a k8s fleet is actually used.
        """
        config = config or {}
        get_settings()  # kept for parity with SwarmDriver (config precedence)
        context = config.get("kube_context") or config.get("context")
        namespace = config.get("namespace") or "stoker"
        return cls(namespace=namespace, context=context)

    # -- lazy client construction (never hit in unit tests) --------------- #

    def _batch_api(self):
        # type: () -> Any
        if self._batch is None:
            self._batch = _build_client("BatchV1Api", self._context)
        return self._batch

    def _core_api(self):
        # type: () -> Any
        if self._core is None:
            self._core = _build_client("CoreV1Api", self._context)
        return self._core

    # -- ExecutionDriver -------------------------------------------------- #

    def create(self, run, workers):
        # type: (RunSnapshot, int) -> DriverRef
        if workers < 1:
            raise DriverError("workers must be >= 1")
        ns = self._namespace
        s_name = secret_name(run.run_id)
        j_name = job_name(run.run_id)

        # 1. Create the per-run Secret carrying the HEC token (if any). Created
        #    first so the Job's secretKeyRef resolves; the ownerReference is
        #    patched on afterwards (we need the Job's uid), so the Secret is GC'd
        #    with the Job. ttlSecondsAfterFinished on the Job is the stray-catcher.
        hec_token = (run.env or {}).get(_HEC_ENV)
        if hec_token:
            secret = self._secret_manifest(run)
            log.info("k8s create secret %s (ns=%s) for run %s",
                     s_name, ns, run.run_id)
            self._call(self._core_api().create_namespaced_secret,
                       namespace=ns, body=secret)

        # 2. Create the Job (HEC token referenced via secretKeyRef, never inline).
        job = self._job_manifest(run, workers)
        log.info("k8s create job %s (ns=%s) desired=%d image=%s",
                 j_name, ns, workers, run.image)
        try:
            created = self._call(self._batch_api().create_namespaced_job,
                                 namespace=ns, body=job)
        except DriverError:
            # The Secret was created but its GC owner (the Job) was not, so
            # neither the ownerReference nor the Job's TTL will ever reap it.
            # Delete the orphaned Secret before re-raising so no credential
            # lingers in the namespace.
            if hec_token:
                self._delete_secret_quietly(ns, s_name)
            raise
        job_uid = _uid_of(created)

        # 3. Adopt the Secret under the Job (ownerReference) so it is garbage-
        #    collected with the Job. Best-effort: a failure here is logged but
        #    the ttlSecondsAfterFinished stray-catcher still bounds the Secret.
        if hec_token and job_uid:
            self._adopt_secret(ns, s_name, j_name, job_uid)

        return DriverRef(
            kind=_KIND,
            id=j_name,
            raw={"run_id": run.run_id, "name": j_name,
                 "namespace": ns, "secret": s_name, "uid": job_uid},
        )

    def scale(self, ref, workers):
        # type: (DriverRef, int) -> None
        if workers < 0:
            raise DriverError("workers must be >= 0")
        ns = self._namespace_of(ref)
        # Elastic Indexed Jobs (k8s >= 1.27): parallelism AND completions move
        # together, else the Job would either never finish (completions > parallelism
        # forever short) or refuse the patch (completions < already-succeeded).
        patch = {"spec": {"parallelism": int(workers), "completions": int(workers)}}
        self._call(self._batch_api().patch_namespaced_job,
                   name=ref.id, namespace=ns, body=patch)
        log.info("k8s scaled job %s desired=%d (parallelism+completions)",
                 ref.id, workers)

    def stop(self, ref, grace_s):
        # type: (DriverRef, int) -> None
        # Draining a k8s fleet == delete the Job with Foreground propagation: the
        # running pods get SIGTERM and drain within terminationGracePeriodSeconds
        # (the control plane also answers `drain` on heartbeats so workers flush
        # first), and the delete does not return server-side until the pods are
        # gone. Unlike swarm there is no "scale to zero then remove" -- a single
        # Foreground delete is the drain. The per-run Secret GCs via its
        # ownerReference on the Job. A later `destroy` from the reaper 404s and is
        # absorbed (idempotent). `grace_s` is passed through as the pods' delete
        # grace so an overriding budget is honoured.
        ns = self._namespace_of(ref)
        try:
            self._call(self._batch_api().delete_namespaced_job,
                       name=ref.id, namespace=ns,
                       propagation_policy="Foreground",
                       grace_period_seconds=int(grace_s))
        except NotFound:
            # Already gone (a prior stop/destroy or TTL): draining is a no-op.
            log.info("k8s stop: job %s already gone", ref.id)
            return
        log.info("k8s stopped (foreground delete) job %s (grace %ds)",
                 ref.id, grace_s)

    def destroy(self, ref):
        # type: (DriverRef) -> None
        # Idempotent: a genuine 404 means the Job is already gone -> success.
        # Foreground propagation cascades the delete to the pods and blocks
        # server-side deletion of the Job until they are gone; the per-run Secret
        # is garbage-collected via its ownerReference on the Job.
        ns = self._namespace_of(ref)
        try:
            self._call(self._batch_api().delete_namespaced_job,
                       name=ref.id, namespace=ns,
                       propagation_policy="Foreground")
        except NotFound:
            log.info("k8s destroy: job %s already gone", ref.id)
            return
        log.info("k8s destroyed job %s", ref.id)

    def status(self, ref):
        # type: (DriverRef) -> DriverStatus
        ns = self._namespace_of(ref)
        try:
            job = self._call(self._batch_api().read_namespaced_job,
                             name=ref.id, namespace=ns)
        except NotFound:
            # Job genuinely gone (destroyed) -> desired 0, matching FakeDriver.
            # Any OTHER failure (timeout, 5xx) propagates from _call as a
            # DriverError: status() must surface "unknown", never report a
            # transient failure as desired=0 (which would orphan a live fleet).
            return DriverStatus(desired=0, running=0, tasks=[])

        desired = _job_parallelism(job)

        # Best-effort pod view: list by the run label. A pod-list failure must not
        # sink the whole status (desired is authoritative); running falls back to
        # the Job's own status.active count.
        tasks = []  # type: List[Dict[str, Any]]
        running = None  # type: Optional[int]
        try:
            pods = self._call(self._core_api().list_namespaced_pod,
                              namespace=ns,
                              label_selector="stoker.run=%s" % _run_label(ref))
            tasks = [_pod_view(p) for p in _items_of(pods)]
            running = sum(1 for t in tasks if t["state"] in _RUNNING_PHASES)
        except DriverError as exc:
            log.warning("k8s status: pod list for %s unavailable: %s", ref.id, exc)
        if running is None:
            running = _job_active(job)
        return DriverStatus(desired=desired, running=running, tasks=tasks)

    def logs(self, ref, slot, tail):
        # type: (DriverRef, Optional[int], int) -> str
        # Pod logs are best-effort observability; never fail a caller over them.
        ns = self._namespace_of(ref)
        try:
            pods = self._call(self._core_api().list_namespaced_pod,
                              namespace=ns,
                              label_selector="stoker.run=%s" % _run_label(ref))
        except DriverError as exc:
            log.warning("k8s logs for %s unavailable: %s", ref.id, exc)
            return ""
        chunks = []  # type: List[str]
        for pod in _items_of(pods):
            pod_slot = _pod_completion_index(pod)
            if slot is not None and pod_slot != slot:
                continue
            name = _pod_name(pod)
            if not name:
                continue
            try:
                text = self._call(
                    self._core_api().read_namespaced_pod_log,
                    name=name, namespace=ns,
                    tail_lines=int(tail) if tail and tail > 0 else None)
            except DriverError as exc:
                log.warning("k8s logs: pod %s unavailable: %s", name, exc)
                continue
            if text:
                chunks.append(text if isinstance(text, str) else str(text))
        return "\n".join(chunks)

    # -- discovery (optional 7th method) ---------------------------------- #

    def list_run_ids(self):
        # type: () -> Set[int]
        """Return every run id this namespace owns, by the ``stoker.run`` label.

        Lists ``batch/v1`` Jobs in the driver's namespace with label selector
        ``stoker.run`` (presence-only: matches any Job carrying the label,
        whatever its value); each Job's ``metadata.labels['stoker.run']`` (or its
        ``stoker-run-<id>`` name as a fallback) parses to the run id. Boot
        reconciliation uses this to spot strays (a labelled Job with no live DB
        run).

        A backend failure propagates from :meth:`_call` as :class:`DriverError`
        (the caller skips the sweep rather than mistaking a hiccup for "no
        strays"); Jobs whose label does not parse to an int are skipped.
        """
        listing = self._call(self._batch_api().list_namespaced_job,
                             namespace=self._namespace,
                             label_selector="stoker.run")
        run_ids = set()  # type: Set[int]
        for job in _items_of(listing):
            run_id = _job_run_id(job)
            if run_id is not None:
                run_ids.add(run_id)
        return run_ids

    # -- ownerReference adoption ------------------------------------------ #

    def _adopt_secret(self, ns, s_name, j_name, job_uid):
        # type: (str, str, str, str) -> None
        """Patch the per-run Secret with an ownerReference to the Job (GC)."""
        owner = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": j_name,
            "uid": job_uid,
            "controller": True,
            "blockOwnerDeletion": True,
        }
        patch = {"metadata": {"ownerReferences": [owner]}}
        try:
            self._call(self._core_api().patch_namespaced_secret,
                       name=s_name, namespace=ns, body=patch)
        except DriverError as exc:
            # ttlSecondsAfterFinished on the Job is the backstop stray-catcher.
            log.warning("k8s: could not set ownerReference on secret %s: %s",
                        s_name, exc)

    def _delete_secret_quietly(self, ns, s_name):
        # type: (str, str) -> None
        """Best-effort delete of a per-run Secret (used to clean up when the Job
        create fails after the Secret was created). Swallows all errors."""
        try:
            self._call(self._core_api().delete_namespaced_secret,
                       name=s_name, namespace=ns)
            log.info("k8s: cleaned up orphaned secret %s (job create failed)", s_name)
        except (NotFound, DriverError) as exc:
            log.warning("k8s: could not clean up orphaned secret %s: %s", s_name, exc)

    # -- manifest construction -------------------------------------------- #

    def _secret_manifest(self, run):
        # type: (RunSnapshot) -> Dict[str, Any]
        """Build the per-run Secret carrying the HEC token in ``stringData``.

        The token is placed in ``stringData`` (not ``data``) so it is never
        base64-mangled in the manifest and, crucially, never lands in the Job's
        pod-spec env. The ownerReference is added post-create (needs the Job uid).
        """
        run_label = str(run.run_id)
        labels = {"stoker.run": run_label}
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": secret_name(run.run_id),
                "namespace": self._namespace,
                "labels": labels,
            },
            "stringData": {_HEC_SECRET_KEY: (run.env or {})[_HEC_ENV]},
        }

    def _job_manifest(self, run, workers):
        # type: (RunSnapshot, int) -> Dict[str, Any]
        """Build the Indexed ``batch/v1`` Job manifest for a run's fleet.

        Env is projected as a list of ``{"name","value"}`` (the HEC token is the
        one exception: it rides in as a ``valueFrom.secretKeyRef``, never a plain
        value). Labels carry ``stoker.run=<id>`` on the Job, the pod template and
        the selector so boot reconciliation and ``status`` can find owned pods.
        """
        run_label = str(run.run_id)
        labels = dict(run.labels or {})
        labels.setdefault("stoker.run", run_label)

        env = self._env_vars(run)
        opts = run.driver_opts or {}

        container = {
            "name": "worker",
            "image": run.image,
            # A floating tag (…:latest) that a node already cached is otherwise
            # served stale, so a freshly pushed worker never reaches the fleet
            # (the same trap the SwarmDriver avoids by resolving the tag to a
            # digest). Always re-pull; a digest-pinned run.image ignores this.
            "imagePullPolicy": "Always",
            "env": env,
        }  # type: Dict[str, Any]
        resources = opts.get("resources")
        if isinstance(resources, dict) and resources:
            container["resources"] = resources

        pod_spec = {
            "restartPolicy": "OnFailure",
            # Workers never call the API server: do not mount a SA token.
            "automountServiceAccountToken": False,
            # Give the drain path room (WORKER-CONTRACT: 45 s SIGTERM budget).
            "terminationGracePeriodSeconds": int(run.stop_grace_s),
            "containers": [container],
        }  # type: Dict[str, Any]
        node_selector = opts.get("node_selector")
        if isinstance(node_selector, dict) and node_selector:
            pod_spec["nodeSelector"] = node_selector

        job_spec = {
            "completionMode": "Indexed",
            "parallelism": int(workers),
            "completions": int(workers),
            # A worker may legitimately restart (spot reclaim, transient HEC);
            # 3*N gives the whole fleet headroom before the Job itself fails.
            "backoffLimit": 3 * int(workers),
            # Reap finished Jobs (and their GC'd Secrets/pods) after an hour so a
            # completed campaign leaves nothing behind.
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": pod_spec,
            },
        }  # type: Dict[str, Any]

        # Bounded runs get a hard deadline = duration + 300 s so a wedged fleet
        # cannot outlive its run (belt-and-braces with the control-plane drain).
        deadline = _active_deadline(run)
        if deadline is not None:
            job_spec["activeDeadlineSeconds"] = deadline

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name(run.run_id),
                "namespace": self._namespace,
                "labels": labels,
            },
            "spec": job_spec,
        }

    def _env_vars(self, run):
        # type: (RunSnapshot) -> List[Dict[str, Any]]
        """Project the worker env as a k8s env list, sorted for determinism.

        Every key except the HEC token becomes ``{"name","value"}``. The HEC
        token is delivered as ``valueFrom.secretKeyRef`` -> the per-run Secret, so
        the plaintext never appears on the pod spec. ``JOB_COMPLETION_INDEX`` (the
        slot hint) is surfaced to the worker via ``fieldRef`` on the pod's
        annotation that Kubernetes stamps with the completion index, mapped to
        ``STOKER_HINT_SLOT``.
        """
        env = run.env or {}
        out = []  # type: List[Dict[str, Any]]
        for key in sorted(env):
            if key == _HEC_ENV:
                continue  # never inline the secret; added via secretKeyRef below
            value = env[key]
            if value is None:
                continue
            out.append({"name": key, "value": str(value)})

        if _HEC_ENV in env and env[_HEC_ENV] is not None:
            out.append({
                "name": _HEC_ENV,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": secret_name(run.run_id),
                        "key": _HEC_SECRET_KEY,
                    }
                },
            })

        # STOKER_HINT_SLOT <- the pod's completion index. Kubernetes stamps the
        # index into the annotation batch.kubernetes.io/job-completion-index on
        # each Indexed-Job pod; expose it via the downward API.
        out.append({
            "name": "STOKER_HINT_SLOT",
            "valueFrom": {
                "fieldRef": {
                    "fieldPath": ("metadata.annotations['batch.kubernetes.io/"
                                  "job-completion-index']"),
                }
            },
        })
        return out

    # -- addressing / call plumbing --------------------------------------- #

    def _namespace_of(self, ref):
        # type: (DriverRef) -> str
        return str((ref.raw or {}).get("namespace") or self._namespace)

    def _call(self, fn, **kwargs):
        # type: (Any, Any) -> Any
        """Invoke a kubernetes client method, mapping its errors to DriverError.

        A genuine 404 becomes :class:`NotFound`; any other API error (5xx,
        timeout, connection failure) becomes :class:`DriverError` so a transient
        hiccup is never mistaken for "gone". The client's ``ApiException`` is
        duck-typed on a ``.status`` attribute so this works whether or not the
        real kubernetes client is importable (unit tests inject a stand-in).
        Secrets never appear in the raised message.
        """
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - translated to DriverError below
            status = getattr(exc, "status", None)
            name = getattr(fn, "__name__", "k8s call")
            if status == 404:
                raise NotFound("k8s %s -> HTTP 404" % name)
            if status is not None:
                raise DriverError("k8s %s -> HTTP %s" % (name, status))
            # Not an API status error: connection reset, kubeconfig problem, etc.
            raise DriverError("k8s %s failed: %s" % (name, exc.__class__.__name__))


# --------------------------------------------------------------------------- #
# Module-level helpers (pure; unit-tested via the mocked-client path).
# --------------------------------------------------------------------------- #

def _build_client(api_name, context):
    # type: (str, Optional[str]) -> Any
    """Construct a real kubernetes API client for ``context`` (lazy import)."""
    from kubernetes import client, config  # imported only on the real path

    config.load_kube_config(context=context)
    return getattr(client, api_name)()


def _run_label(ref):
    # type: (DriverRef) -> str
    raw = ref.raw or {}
    run_id = raw.get("run_id")
    if run_id is not None:
        return str(run_id)
    # Fall back to the Job name suffix (stoker-run-<id>).
    name = raw.get("name") or ref.id or ""
    return name.rsplit("-", 1)[-1] if name else ""


def _job_run_id(job):
    # type: (Any) -> Optional[int]
    """Parse a k8s Job object/dict to its ``stoker.run`` run id, or None.

    Prefers ``metadata.labels['stoker.run']`` (the authoritative marker the
    driver stamps on create); falls back to the ``stoker-run-<id>`` Job-name
    suffix when the label is missing. A non-integer value yields None (skip it —
    the sweep must never destroy on a guessed id).
    """
    meta = _attr(job, "metadata")
    labels = _attr(meta, "labels") or {}
    raw = labels.get("stoker.run") if isinstance(labels, dict) else None
    if raw is None:
        name = _attr(meta, "name") or ""
        if isinstance(name, str) and name.startswith("stoker-run-"):
            raw = name[len("stoker-run-"):]
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _active_deadline(run):
    # type: (RunSnapshot) -> Optional[int]
    """activeDeadlineSeconds = duration + 300 for bounded runs, else None.

    The bounded duration is read from ``driver_opts['duration_s']`` (the
    lifecycle passes it through); an absent/zero/unbounded duration yields None so
    the Job has no hard deadline.
    """
    opts = run.driver_opts or {}
    duration = opts.get("duration_s")
    if duration is None:
        return None
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return int(duration) + 300


def _uid_of(job):
    # type: (Any) -> str
    """Extract ``.metadata.uid`` from a created Job (object or dict)."""
    meta = _attr(job, "metadata")
    uid = _attr(meta, "uid")
    return str(uid) if uid else ""


def _job_parallelism(job):
    # type: (Any) -> int
    """Desired replicas = the Job's ``spec.parallelism`` (default 0)."""
    spec = _attr(job, "spec")
    value = _attr(spec, "parallelism")
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _job_active(job):
    # type: (Any) -> int
    """Fallback running count = the Job's ``status.active`` (default 0)."""
    status = _attr(job, "status")
    value = _attr(status, "active")
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _items_of(listing):
    # type: (Any) -> List[Any]
    """Return ``.items`` from a k8s list response (object or dict)."""
    items = _attr(listing, "items")
    if items is None:
        return []
    return list(items)


def _pod_view(pod):
    # type: (Any) -> Dict[str, Any]
    """Fold one pod into the best-effort ``{slot,holder,node,state}`` view.

    ``slot`` is the pod's Indexed-Job completion index (observability only; the
    lease is identity). ``holder`` is the pod name, ``node`` the assigned node,
    ``state`` the pod phase (``Running``/``Pending``/``Succeeded``/...).
    """
    meta = _attr(pod, "metadata")
    spec = _attr(pod, "spec")
    status = _attr(pod, "status")
    return {
        "slot": _pod_completion_index(pod),
        "holder": _attr(meta, "name"),
        "node": _attr(spec, "node_name") or _attr(spec, "nodeName"),
        "state": _attr(status, "phase"),
    }


def _pod_name(pod):
    # type: (Any) -> Optional[str]
    return _attr(_attr(pod, "metadata"), "name")


def _pod_completion_index(pod):
    # type: (Any) -> Optional[int]
    """The pod's Indexed-Job completion index from its annotation, or None."""
    meta = _attr(pod, "metadata")
    annotations = _attr(meta, "annotations") or {}
    if not isinstance(annotations, dict):
        return None
    raw = (annotations.get("batch.kubernetes.io/job-completion-index")
           or annotations.get("batch.alpha.kubernetes.io/job-completion-index"))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _attr(obj, name):
    # type: (Any, str) -> Any
    """Read ``name`` from an object attribute or a dict key (tolerates both).

    The real kubernetes client returns typed model objects (attribute access);
    the mocked-client unit tests and any dict-shaped fake use mapping access.
    Supporting both keeps the driver testable without the heavy client.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


__all__ = ["K8sDriver", "job_name", "secret_name"]
