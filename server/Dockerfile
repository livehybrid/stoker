# syntax=docker/dockerfile:1
# Stoker control plane image (FastAPI + the built React UI).
# Build from the repo root:
#   docker buildx build -f server/Dockerfile -t stoker-control-plane .

# --- Stage 1: build the web UI to ui/dist ---------------------------------- #
# Pin the build stage to the native builder arch: the UI output is
# arch-independent JS, so multi-arch builds never run npm under QEMU emulation.
FROM --platform=$BUILDPLATFORM node:20-slim AS ui-build
WORKDIR /ui
# Install deps against the committed lockfile first (cached across source edits).
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build      # -> /ui/dist

# --- Stage 2: the runtime image -------------------------------------------- #
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# git is needed at runtime by the repo-sync engine (subprocess git).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin stoker

WORKDIR /app

# Dependency layer first so source-only changes reuse the pip layer.
COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Worker runtime deps (requests, jinja2, ... — small, no version conflicts with
# the server set) so the in-process fleet (STOKER_INPROCESS_FLEET=1) can spawn
# real stoker_agent workers in this container. Separate layer: worker dep bumps
# never invalidate the server pip layer above.
COPY worker/requirements.txt /app/worker-requirements.txt
RUN pip install --no-cache-dir -r /app/worker-requirements.txt

# Application source under the `server` package (imported as `server.app`).
COPY server /app/server

# The worker source, laid out exactly like the worker image (agent package +
# vendored engines as siblings — the agent resolves engine roots relative to
# its own location). PYTHONPATH makes stoker_agent importable both for the
# in-process fleet's workers and for the server's own pack.yaml parsing
# (bundles reuses the worker's parser when importable).
COPY worker/stoker_agent /app/worker/stoker_agent
COPY worker/engines /app/worker/engines
ENV PYTHONPATH=/app/worker

# The bundled starter packs: registered (and re-linted) at every boot, so a
# fresh deployment has runnable packs with no sideloading (unset the env to
# opt out). Read-only at runtime; bundles are built from these paths directly.
COPY packs /app/packs
ENV STOKER_BUILTIN_PACKS_DIR=/app/packs

# The built UI from stage 1; app.py serves it from ui/dist (server/../ui/dist).
COPY --from=ui-build /ui/dist /app/ui/dist

# Bundles and repo clones live on mounted volumes in prod (config BUNDLE_DIR /
# REPO_CLONE_DIR); create the defaults so a bare run still works.
RUN mkdir -p /data/bundles /data/repos && chown -R stoker:stoker /data

EXPOSE 8080
USER stoker

# uvicorn entry: server/app.py exposes `app = create_app()`.
# --timeout-keep-alive 75: workers heartbeat every ~5 s over a keep-alive
# connection; uvicorn's DEFAULT 5 s keep-alive timeout closes the idle socket
# right between heartbeats, and the reused socket then fails with
# RemoteDisconnected ("heartbeat missed"). Holding idle connections well past
# the heartbeat interval removes that collision. Override via UVICORN_KEEP_ALIVE.
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive ${UVICORN_KEEP_ALIVE:-75}"]
