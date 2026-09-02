"""Environment-driven configuration for the Stoker control plane.

``Settings`` is a frozen dataclass parsed once from the process environment
(the contract's Config section). Secret fields (master key, Portainer token,
dogfood HEC token) are excluded from ``repr`` so an accidental log of the
settings object never leaks them. Call :func:`get_settings` for the cached
singleton; tests build a fresh one with :func:`load_settings(env=...)`.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import logging
import os
from typing import Any, Mapping, Optional, Tuple

log = logging.getLogger("stoker.config")

# Default local dev DB: SQLite in the working directory. Prod overrides with a
# postgresql+psycopg:// URL via DATABASE_URL.
DEFAULT_DATABASE_URL = "sqlite:///./stoker.db"
DEFAULT_JWT_TTL_S = 3600
# Portainer environment (endpoint) id. There is no universal value: it is the id
# of YOUR Portainer environment, so it must be set per install (the swarm fleet
# 404s at provision if it is wrong). 6 is only the author's; treat it as a
# placeholder and override with PORTAINER_ENDPOINT / the fleet config.
DEFAULT_PORTAINER_ENDPOINT = 6
# Verify the Portainer server's TLS certificate. Default False because a homelab
# Portainer usually serves a self-signed cert; set PORTAINER_VERIFY_TLS=1 (and a
# CA the host trusts) in production so the tier-0 API key is not sent over an
# unverified channel. A per-fleet ``verify_tls`` in the fleet config overrides it.
DEFAULT_PORTAINER_VERIFY_TLS = False
DEFAULT_BUNDLE_DIR = "/data/bundles"
DEFAULT_REPO_CLONE_DIR = "/data/repos"
DEFAULT_PORT = 8080
# Local default image tag; prod pins ghcr.io/livehybrid/stoker-worker@sha256:...
DEFAULT_WORKER_IMAGE = "ghcr.io/livehybrid/stoker-worker:latest"

# Auth defaults. The operator session cookie lives 12 h by default; the trusted
# proxy header defaults to the de-facto standard emitted by Traefik/authentik
# forward-auth; a proxy-asserted user with no explicit role becomes an operator.
DEFAULT_SESSION_TTL_S = 43200  # 12 hours
DEFAULT_AUTH_HEADER = "X-Forwarded-User"
DEFAULT_PROXY_ROLE = "operator"

# Metric-sample maintenance windows (roll-up + prune). A long soak at many
# workers appends metric_samples every ~5 s; the maintenance loop down-samples
# fine-grained rows older than the roll-up window to one row per slot per 60 s
# bucket, then deletes anything older than the prune window entirely.
DEFAULT_METRIC_ROLLUP_AFTER_H = 48    # down-sample rows older than 48 h
DEFAULT_METRIC_PRUNE_AFTER_D = 30     # delete rows older than 30 days
DEFAULT_METRIC_ROLLUP_BUCKET_S = 60   # target bucket width for the roll-up
DEFAULT_METRIC_MAINTENANCE_INTERVAL_S = 3600.0  # run the slow loop ~hourly
DEFAULT_METRIC_DELETE_CHUNK = 5000    # rows per delete batch (never one huge txn)

# Dogfood telemetry cadence: the per-active-run aggregate emitter ships a
# stoker:metrics event this often when dogfood is enabled.
DEFAULT_DOGFOOD_METRICS_INTERVAL_S = 30.0

# rawreplay (Piston) dataset fetch: a pack.yaml `dataset_url` (e.g. an
# attack_data capture) is downloaded at bundle-build time. https only, capped so
# a hostile/huge URL cannot exhaust the control-plane disk, and sha-verifiable
# when the pack declares a `dataset_sha256`.
DEFAULT_RAWREPLAY_MAX_DATASET_BYTES = 512 * 1024 * 1024  # 512 MiB
DEFAULT_RAWREPLAY_FETCH_TIMEOUT_S = 120.0

# Pack upload (POST /api/packs/upload): where extracted packs land, and the
# resource caps on the UNTRUSTED archive an operator uploads. The directory
# sits beside BUNDLE_DIR / REPO_CLONE_DIR on the control-plane volume so
# uploaded packs survive a restart (mount a volume over /data). The caps are
# extraction-bomb defences (server/packupload.py enforces them while streaming,
# never from the archive's own declared sizes): the archive body itself, the
# total uncompressed payload, any single member, and the member count. All are
# deliberately generous for a real pack (conf + samples, or a rawreplay
# dataset) while bounding what a hostile archive can make the control plane
# hold; the member cap mirrors DEFAULT_RAWREPLAY_MAX_DATASET_BYTES, the
# largest single legitimate pack file we expect.
DEFAULT_PACK_UPLOAD_DIR = "/data/uploads"
DEFAULT_PACK_UPLOAD_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024  # 256 MiB upload body
DEFAULT_PACK_UPLOAD_MAX_TOTAL_BYTES = 1024 * 1024 * 1024   # 1 GiB unpacked total
DEFAULT_PACK_UPLOAD_MAX_MEMBER_BYTES = 512 * 1024 * 1024   # 512 MiB per member
DEFAULT_PACK_UPLOAD_MAX_MEMBERS = 10_000
# The three authorisation roles, most to least privileged. ``admin`` gates user
# management; validated here so a bad STOKER_PROXY_DEFAULT_ROLE fails at boot.
VALID_ROLES = ("viewer", "operator", "admin")

# Namespace the seeded ``k8s-local`` fleet runs Jobs in (env K8S_NAMESPACE).
# Matches the EKS Terraform module's default namespace.
DEFAULT_K8S_NAMESPACE = "stoker"

# Worker cap for the in-process fleet (workers run INSIDE the control-plane
# container, sharing its CPU/memory — small workloads only by design).
DEFAULT_INPROCESS_MAX_WORKERS = 2

# Per-worker rate ceilings (engines/ceilings.py). The built-in table (25 GB/day,
# 5000 EPS per worker) is deliberately conservative; a real worker often beats
# it when the data is cheap to template and Splunk is close on the network. The
# env can therefore raise (or disable) it globally: STOKER_MAX_EPS_PER_WORKER /
# STOKER_MAX_GB_DAY_PER_WORKER set the default for every engine, and a
# per-engine suffix (e.g. STOKER_MAX_GB_DAY_PER_WORKER_EVENTGEN) narrows it to
# one engine. Unset = the built-in default; 0 (or negative) = that bound is
# DISABLED entirely. A per-fleet config_json override (same key names) still
# beats all of these — resolution lives in engines.ceilings.resolve_ceilings.
_CEILING_ENV_PREFIXES = (
    ("STOKER_MAX_EPS_PER_WORKER_", "max_eps_per_worker"),
    ("STOKER_MAX_GB_DAY_PER_WORKER_", "max_gb_day_per_worker"),
)


class ConfigError(Exception):
    """Raised when an environment value cannot be parsed."""


@dataclasses.dataclass(frozen=True)
class Settings:
    """Frozen runtime settings. Secret fields are ``repr=False``."""

    database_url: str
    # Fernet key (urlsafe base64, 32 bytes). Secret: never logged.
    master_key: str = dataclasses.field(repr=False)
    jwt_ttl_s: int
    public_base_url: str
    worker_image: str
    portainer_host: Optional[str]
    # Portainer tier-0 API key. Secret: never logged.
    portainer_token: Optional[str] = dataclasses.field(repr=False)
    portainer_endpoint: int
    bundle_dir: str
    dogfood_hec_url: Optional[str]
    # Dogfood HEC token for optional self-telemetry. Secret: never logged.
    dogfood_hec_token: Optional[str] = dataclasses.field(repr=False)
    port: int
    # True when master_key was auto-generated (dev) rather than supplied.
    master_key_generated: bool = False
    # Where repo clones live in the control-plane volume (git-sync, stage 3).
    # Defaulted and last so existing Settings(...) call sites stay valid.
    repo_clone_dir: str = DEFAULT_REPO_CLONE_DIR
    # Verify the Portainer TLS certificate (env PORTAINER_VERIFY_TLS). Defaulted
    # so existing Settings(...) call sites stay valid; a per-fleet ``verify_tls``
    # still overrides it in SwarmDriver.from_fleet_config.
    portainer_verify_tls: bool = DEFAULT_PORTAINER_VERIFY_TLS

    # --- Auth (all defaulted so existing Settings(...) call sites stay valid) --
    # Bootstrap admin from the environment: if both are set and the user is
    # absent at startup, it is created as an admin. Password is a secret.
    admin_user: Optional[str] = None
    admin_password: Optional[str] = dataclasses.field(default=None, repr=False)
    # Lifetime of the signed operator session cookie, in seconds.
    session_ttl_s: int = DEFAULT_SESSION_TTL_S
    # Networks whose members are trusted to assert the auth header (the reverse
    # proxy). Empty => no proxy is trusted, so the header is always ignored.
    trusted_proxies: Tuple[Any, ...] = ()
    # Header a trusted proxy uses to carry the authenticated username.
    auth_header: str = DEFAULT_AUTH_HEADER
    # Role assigned to a proxy-asserted user on first sight.
    proxy_default_role: str = DEFAULT_PROXY_ROLE
    # Kill switch for local dev: skip auth entirely (with a loud warning).
    auth_disabled: bool = False

    # --- Metric-sample maintenance (roll-up + prune) ------------------------ #
    # All defaulted so existing Settings(...) call sites (incl. tests) stay valid.
    metric_rollup_after_h: int = DEFAULT_METRIC_ROLLUP_AFTER_H
    metric_prune_after_d: int = DEFAULT_METRIC_PRUNE_AFTER_D
    metric_rollup_bucket_s: int = DEFAULT_METRIC_ROLLUP_BUCKET_S
    metric_maintenance_interval_s: float = DEFAULT_METRIC_MAINTENANCE_INTERVAL_S
    metric_delete_chunk: int = DEFAULT_METRIC_DELETE_CHUNK

    # --- Dogfood telemetry -------------------------------------------------- #
    # Cadence of the periodic per-run stoker:metrics aggregate (dogfood only).
    dogfood_metrics_interval_s: float = DEFAULT_DOGFOOD_METRICS_INTERVAL_S
    # Gzip the HEC event batch body (best-effort; the collector accepts either).
    dogfood_gzip: bool = True

    # --- rawreplay (Piston) dataset fetch ----------------------------------- #
    # Cap on a `dataset_url` download at bundle-build time (bytes); a fetch that
    # would exceed this is refused. Fetch timeout in seconds.
    rawreplay_max_dataset_bytes: int = DEFAULT_RAWREPLAY_MAX_DATASET_BYTES
    rawreplay_fetch_timeout_s: float = DEFAULT_RAWREPLAY_FETCH_TIMEOUT_S

    # --- Pack upload (archives via the operator API) ------------------------- #
    # Where uploaded packs are extracted to (env PACK_UPLOAD_DIR; a persistent
    # volume in prod, so uploads survive a restart) and the extraction-bomb caps
    # (see the DEFAULT_PACK_UPLOAD_* comment above). All defaulted so existing
    # Settings(...) call sites stay valid.
    pack_upload_dir: str = DEFAULT_PACK_UPLOAD_DIR
    pack_upload_max_archive_bytes: int = DEFAULT_PACK_UPLOAD_MAX_ARCHIVE_BYTES
    pack_upload_max_total_bytes: int = DEFAULT_PACK_UPLOAD_MAX_TOTAL_BYTES
    pack_upload_max_member_bytes: int = DEFAULT_PACK_UPLOAD_MAX_MEMBER_BYTES
    pack_upload_max_members: int = DEFAULT_PACK_UPLOAD_MAX_MEMBERS

    # --- Kubernetes fleets --------------------------------------------------- #
    # Namespace the seeded ``k8s-local`` fleet runs worker Jobs in (env
    # K8S_NAMESPACE); per-fleet ``config_json.namespace`` still overrides it.
    k8s_namespace: str = DEFAULT_K8S_NAMESPACE
    # Default nodeSelector for the seeded ``k8s-local`` fleet (env
    # K8S_NODE_SELECTOR, ``key=value[,key=value...]``), stored as sorted pairs
    # for the frozen dataclass. Seeded into the fleet's config_json at first
    # boot so a fresh deployment lands worker Jobs on dedicated node groups
    # declaratively; a spec's ``driver_opts.node_selector`` overrides per run.
    k8s_node_selector: Tuple[Tuple[str, str], ...] = ()

    # --- In-process fleet (workers inside the control-plane container) ------- #
    # Opt-in (env STOKER_INPROCESS_FLEET): seeds the ``inprocess-local`` fleet,
    # whose runs spawn real stoker_agent subprocesses in this container — no
    # swarm/k8s backend at all. Small workloads only: workers share the control
    # plane's CPU/memory, so the worker count is capped (env
    # STOKER_INPROCESS_MAX_WORKERS) and custom-code packs are refused at submit.
    inprocess_fleet_enabled: bool = False
    inprocess_max_workers: int = DEFAULT_INPROCESS_MAX_WORKERS

    # --- Per-worker rate ceilings -------------------------------------------- #
    # Env-configured defaults for the submit-time ceiling check (see the
    # _CEILING_ENV_PREFIXES comment above). None = unset (the built-in table in
    # engines/ceilings.py applies); <= 0 = that bound is disabled entirely.
    # ``per_engine_ceilings`` holds the per-engine env overrides as sorted
    # (engine, bound_key, raw_value) triples (a tuple so the dataclass stays
    # hashable/frozen-friendly, like ``trusted_proxies``).
    max_eps_per_worker: Optional[float] = None
    max_gb_day_per_worker: Optional[float] = None
    per_engine_ceilings: Tuple[Tuple[str, str, float], ...] = ()

    # --- Builtin packs ------------------------------------------------------- #
    # A directory of pack roots registered (and re-linted) at every boot, so the
    # image's bundled starter packs appear in the UI with no sideloading (env
    # STOKER_BUILTIN_PACKS_DIR; the server image sets /app/packs). None/empty =
    # no builtin seeding.
    builtin_packs_dir: Optional[str] = None

    @property
    def is_sqlite(self):
        # type: () -> bool
        return self.database_url.startswith("sqlite")

    @property
    def dogfood_enabled(self):
        # type: () -> bool
        return bool(self.dogfood_hec_url and self.dogfood_hec_token)

    @property
    def proxy_trust_enabled(self):
        # type: () -> bool
        """True when at least one trusted proxy network is configured.

        Proxy (SSO) auth is only wired when the operator has declared which
        peers may assert the auth header; with none, the header is untrusted.
        """
        return bool(self.trusted_proxies)


def _get(env, key, default=None):
    # type: (Mapping[str, str], str, Optional[str]) -> Optional[str]
    val = env.get(key)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


def _get_int(env, key, default):
    # type: (Mapping[str, str], str, int) -> int
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("%s must be an integer, got %r" % (key, raw))


def _get_float(env, key, default):
    # type: (Mapping[str, str], str, float) -> float
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError("%s must be a number, got %r" % (key, raw))


def _get_opt_float(env, key):
    # type: (Mapping[str, str], str) -> Optional[float]
    """Parse an optional float env var (unset/blank -> None, not a default)."""
    raw = _get(env, key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ConfigError("%s must be a number, got %r" % (key, raw))


def _parse_node_selector(env):
    # type: (Mapping[str, str]) -> Tuple[Tuple[str, str], ...]
    """Parse ``K8S_NODE_SELECTOR`` (``key=value[,key=value...]``) into pairs.

    The seeded ``k8s-local`` fleet stores these as its default nodeSelector so
    a fresh deployment places worker Jobs on dedicated node groups with no
    per-spec step. A malformed entry is a hard :class:`ConfigError` at boot
    (matching the numeric settings); blank/unset means no selector.
    """
    raw = _get(env, "K8S_NODE_SELECTOR")
    if not raw:
        return ()
    pairs = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ConfigError(
                "K8S_NODE_SELECTOR entries must be key=value, got %r" % part)
        pairs.append((key.strip(), value.strip()))
    return tuple(pairs)


def _parse_engine_ceilings(env):
    # type: (Mapping[str, str]) -> Tuple[Tuple[str, str, float], ...]
    """Scan the env for per-engine ceiling overrides.

    ``STOKER_MAX_EPS_PER_WORKER_<ENGINE>`` / ``STOKER_MAX_GB_DAY_PER_WORKER_<ENGINE>``
    (engine name uppercased, e.g. ``..._EVENTGEN``) override the global ceiling
    for that one engine. Returned as sorted (engine, bound_key, value) triples;
    a malformed value is a hard :class:`ConfigError` at boot, matching the other
    numeric settings. Blank values are ignored (treated as unset).
    """
    entries = []
    for env_key in env:
        for prefix, bound_key in _CEILING_ENV_PREFIXES:
            if env_key.startswith(prefix) and len(env_key) > len(prefix):
                value = _get_opt_float(env, env_key)
                if value is None:
                    continue
                entries.append((env_key[len(prefix):].lower(), bound_key, value))
    return tuple(sorted(entries))


def _get_bool(env, key, default=False):
    # type: (Mapping[str, str], str, bool) -> bool
    """Parse a boolean env var (1/true/yes/on are true; unset -> default)."""
    raw = _get(env, key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_trusted_proxies(raw):
    # type: (Optional[str]) -> Tuple[Any, ...]
    """Parse a comma-separated CIDR/IP list into ``ip_network`` objects.

    Each entry may be a CIDR (``10.0.0.0/8``) or a bare address (``172.20.0.5``,
    normalised to a /32 or /128). ``strict=False`` tolerates host bits set. A
    malformed entry is a hard :class:`ConfigError` at boot so a typo can never
    silently widen or void the trust boundary.
    """
    if not raw:
        return ()
    nets = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ConfigError(
                "STOKER_TRUSTED_PROXIES entry %r is not a valid IP/CIDR: %s"
                % (entry, exc)
            )
    return tuple(nets)


def _generate_master_key():
    # type: () -> str
    """Generate a throwaway Fernet key for local dev with a loud warning."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    log.warning(
        "STOKER_MASTER_KEY not set: generated an ephemeral dev key. "
        "Encrypted secrets will NOT survive a restart. Set STOKER_MASTER_KEY "
        "in production."
    )
    return key


def load_settings(env=None):
    # type: (Optional[Mapping[str, str]]) -> Settings
    """Parse the environment into a frozen :class:`Settings`.

    Unset optional values fall back to the documented defaults. An unset
    ``STOKER_MASTER_KEY`` triggers a generated dev key (with a warning) rather
    than failing, so the app boots out of the box locally.
    """
    if env is None:
        env = os.environ

    database_url = _get(env, "DATABASE_URL", DEFAULT_DATABASE_URL) or DEFAULT_DATABASE_URL

    # The master key protects every Fernet-encrypted secret (target tokens, repo
    # credentials). Prefer a file mount (STOKER_MASTER_KEY_FILE, e.g. a swarm
    # secret at /run/secrets/...) over an env var; fall back to the env var, then
    # to a generated dev key (with a loud warning).
    master_key = _get(env, "STOKER_MASTER_KEY")
    key_file = _get(env, "STOKER_MASTER_KEY_FILE")
    if master_key is None and key_file:
        try:
            with open(key_file, "r", encoding="utf-8") as fh:
                master_key = fh.read().strip() or None
        except OSError as exc:
            raise ConfigError("STOKER_MASTER_KEY_FILE %r unreadable: %s"
                              % (key_file, exc))
    master_key_generated = master_key is None
    if master_key is None:
        master_key = _generate_master_key()

    public_base_url = _get(env, "PUBLIC_BASE_URL")
    if not public_base_url:
        port = _get_int(env, "PORT", DEFAULT_PORT)
        public_base_url = "http://localhost:%d" % port
    public_base_url = public_base_url.rstrip("/")

    # --- Auth --------------------------------------------------------------- #
    trusted_proxies = _parse_trusted_proxies(_get(env, "STOKER_TRUSTED_PROXIES"))
    proxy_default_role = (_get(env, "STOKER_PROXY_DEFAULT_ROLE", DEFAULT_PROXY_ROLE)
                          or DEFAULT_PROXY_ROLE)
    if proxy_default_role not in VALID_ROLES:
        raise ConfigError(
            "STOKER_PROXY_DEFAULT_ROLE must be one of %s, got %r"
            % (", ".join(VALID_ROLES), proxy_default_role))
    auth_header = _get(env, "STOKER_AUTH_HEADER", DEFAULT_AUTH_HEADER) or DEFAULT_AUTH_HEADER

    return Settings(
        database_url=database_url,
        master_key=master_key,
        master_key_generated=master_key_generated,
        jwt_ttl_s=_get_int(env, "STOKER_JWT_TTL_S", DEFAULT_JWT_TTL_S),
        public_base_url=public_base_url,
        worker_image=_get(env, "WORKER_IMAGE", DEFAULT_WORKER_IMAGE) or DEFAULT_WORKER_IMAGE,
        portainer_host=_get(env, "PORTAINER_HOST"),
        portainer_token=_get(env, "PORTAINER_TOKEN"),
        portainer_endpoint=_get_int(env, "PORTAINER_ENDPOINT", DEFAULT_PORTAINER_ENDPOINT),
        portainer_verify_tls=_get_bool(env, "PORTAINER_VERIFY_TLS",
                                       DEFAULT_PORTAINER_VERIFY_TLS),
        bundle_dir=_get(env, "BUNDLE_DIR", DEFAULT_BUNDLE_DIR) or DEFAULT_BUNDLE_DIR,
        repo_clone_dir=_get(env, "REPO_CLONE_DIR", DEFAULT_REPO_CLONE_DIR) or DEFAULT_REPO_CLONE_DIR,
        dogfood_hec_url=_get(env, "DOGFOOD_HEC_URL"),
        dogfood_hec_token=_get(env, "DOGFOOD_HEC_TOKEN"),
        port=_get_int(env, "PORT", DEFAULT_PORT),
        admin_user=_get(env, "STOKER_ADMIN_USER"),
        admin_password=_get(env, "STOKER_ADMIN_PASSWORD"),
        session_ttl_s=_get_int(env, "STOKER_SESSION_TTL", DEFAULT_SESSION_TTL_S),
        trusted_proxies=trusted_proxies,
        auth_header=auth_header,
        proxy_default_role=proxy_default_role,
        auth_disabled=_get_bool(env, "STOKER_AUTH_DISABLED", False),
        metric_rollup_after_h=_get_int(env, "METRIC_ROLLUP_AFTER_H",
                                       DEFAULT_METRIC_ROLLUP_AFTER_H),
        metric_prune_after_d=_get_int(env, "METRIC_PRUNE_AFTER_D",
                                      DEFAULT_METRIC_PRUNE_AFTER_D),
        metric_rollup_bucket_s=_get_int(env, "METRIC_ROLLUP_BUCKET_S",
                                        DEFAULT_METRIC_ROLLUP_BUCKET_S),
        metric_maintenance_interval_s=_get_float(
            env, "METRIC_MAINTENANCE_INTERVAL_S",
            DEFAULT_METRIC_MAINTENANCE_INTERVAL_S),
        metric_delete_chunk=_get_int(env, "METRIC_DELETE_CHUNK",
                                     DEFAULT_METRIC_DELETE_CHUNK),
        dogfood_metrics_interval_s=_get_float(
            env, "DOGFOOD_METRICS_INTERVAL_S",
            DEFAULT_DOGFOOD_METRICS_INTERVAL_S),
        dogfood_gzip=_get_bool(env, "DOGFOOD_GZIP", True),
        rawreplay_max_dataset_bytes=_get_int(
            env, "RAWREPLAY_MAX_DATASET_BYTES",
            DEFAULT_RAWREPLAY_MAX_DATASET_BYTES),
        rawreplay_fetch_timeout_s=_get_float(
            env, "RAWREPLAY_FETCH_TIMEOUT_S",
            DEFAULT_RAWREPLAY_FETCH_TIMEOUT_S),
        pack_upload_dir=_get(env, "PACK_UPLOAD_DIR", DEFAULT_PACK_UPLOAD_DIR)
        or DEFAULT_PACK_UPLOAD_DIR,
        pack_upload_max_archive_bytes=_get_int(
            env, "PACK_UPLOAD_MAX_ARCHIVE_BYTES",
            DEFAULT_PACK_UPLOAD_MAX_ARCHIVE_BYTES),
        pack_upload_max_total_bytes=_get_int(
            env, "PACK_UPLOAD_MAX_TOTAL_BYTES",
            DEFAULT_PACK_UPLOAD_MAX_TOTAL_BYTES),
        pack_upload_max_member_bytes=_get_int(
            env, "PACK_UPLOAD_MAX_MEMBER_BYTES",
            DEFAULT_PACK_UPLOAD_MAX_MEMBER_BYTES),
        pack_upload_max_members=_get_int(
            env, "PACK_UPLOAD_MAX_MEMBERS", DEFAULT_PACK_UPLOAD_MAX_MEMBERS),
        k8s_namespace=_get(env, "K8S_NAMESPACE", DEFAULT_K8S_NAMESPACE)
        or DEFAULT_K8S_NAMESPACE,
        k8s_node_selector=_parse_node_selector(env),
        inprocess_fleet_enabled=_get_bool(env, "STOKER_INPROCESS_FLEET", False),
        inprocess_max_workers=max(1, _get_int(
            env, "STOKER_INPROCESS_MAX_WORKERS", DEFAULT_INPROCESS_MAX_WORKERS)),
        builtin_packs_dir=_get(env, "STOKER_BUILTIN_PACKS_DIR"),
        max_eps_per_worker=_get_opt_float(env, "STOKER_MAX_EPS_PER_WORKER"),
        max_gb_day_per_worker=_get_opt_float(env, "STOKER_MAX_GB_DAY_PER_WORKER"),
        per_engine_ceilings=_parse_engine_ceilings(env),
    )


_SETTINGS = None  # type: Optional[Settings]


def get_settings():
    # type: () -> Settings
    """Return the process-wide cached :class:`Settings` (parsed once)."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def set_settings(settings):
    # type: (Settings) -> None
    """Override the cached settings (tests use this to inject a temp config)."""
    global _SETTINGS
    _SETTINGS = settings


def reset_settings():
    # type: () -> None
    """Clear the cache so the next :func:`get_settings` re-parses the env."""
    global _SETTINGS
    _SETTINGS = None
