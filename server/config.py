"""Environment-driven configuration for the Stoker control plane.

``Settings`` is a frozen dataclass parsed once from the process environment
(the contract's Config section). Secret fields (master key, Portainer token,
dogfood HEC token) are excluded from ``repr`` so an accidental log of the
settings object never leaks them. Call :func:`get_settings` for the cached
singleton; tests build a fresh one with :func:`load_settings(env=...)`.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Mapping, Optional

log = logging.getLogger("stoker.config")

# Default local dev DB: SQLite in the working directory. Prod overrides with a
# postgresql+psycopg:// URL via DATABASE_URL.
DEFAULT_DATABASE_URL = "sqlite:///./stoker.db"
DEFAULT_JWT_TTL_S = 3600
DEFAULT_PORTAINER_ENDPOINT = 6
DEFAULT_BUNDLE_DIR = "/data/bundles"
DEFAULT_REPO_CLONE_DIR = "/data/repos"
DEFAULT_PORT = 8080
# Local default image tag; prod pins ghcr.io/livehybrid/stoker-worker@sha256:...
DEFAULT_WORKER_IMAGE = "ghcr.io/livehybrid/stoker-worker:latest"


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

    @property
    def is_sqlite(self):
        # type: () -> bool
        return self.database_url.startswith("sqlite")

    @property
    def dogfood_enabled(self):
        # type: () -> bool
        return bool(self.dogfood_hec_url and self.dogfood_hec_token)


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

    master_key = _get(env, "STOKER_MASTER_KEY")
    master_key_generated = master_key is None
    if master_key is None:
        master_key = _generate_master_key()

    public_base_url = _get(env, "PUBLIC_BASE_URL")
    if not public_base_url:
        port = _get_int(env, "PORT", DEFAULT_PORT)
        public_base_url = "http://localhost:%d" % port
    public_base_url = public_base_url.rstrip("/")

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
        bundle_dir=_get(env, "BUNDLE_DIR", DEFAULT_BUNDLE_DIR) or DEFAULT_BUNDLE_DIR,
        repo_clone_dir=_get(env, "REPO_CLONE_DIR", DEFAULT_REPO_CLONE_DIR) or DEFAULT_REPO_CLONE_DIR,
        dogfood_hec_url=_get(env, "DOGFOOD_HEC_URL"),
        dogfood_hec_token=_get(env, "DOGFOOD_HEC_TOKEN"),
        port=_get_int(env, "PORT", DEFAULT_PORT),
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
