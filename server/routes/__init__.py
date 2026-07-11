"""HTTP routers for the control plane.

* ``routes.agent`` exports ``router`` — the agent-facing API under ``/api/agent``
  (per-run JWT bearer), which must match the worker's wire protocol exactly.
* ``routes.api`` exports ``router`` — the operator API under ``/api``
  (unauthenticated behind the Traefik LAN allowlist this stage).

``app.py`` registers both by importing the ``router`` object from each module;
the router object names are stable so feature builders fill the endpoint bodies
without touching ``app.py``.
"""

__all__ = []
