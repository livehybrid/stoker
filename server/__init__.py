"""Stoker control plane.

FastAPI + Postgres control plane for the Stoker load-generation fleet. The
DB is the source of truth; the control plane never generates load itself. See
``server/CONTROL-PLANE.md`` for the build contract and
``docs/WORKER-CONTRACT.md`` for the agent-side wire protocol this server must
match byte-for-byte.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
