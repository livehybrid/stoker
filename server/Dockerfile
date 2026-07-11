# syntax=docker/dockerfile:1
# Stoker control plane image.
# Build from the repo root:
#   docker buildx build -f server/Dockerfile -t stoker-control-plane .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin stoker

WORKDIR /app

# Dependency layer first so source-only changes reuse the pip layer.
COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application source under the `server` package (imported as `server.app`).
COPY server /app/server

# The built UI is optional and arrives in a later stage; COPY it when present so
# the app can serve ui/dist. The trailing "." keeps the build working when the
# directory is absent (a bare COPY of a missing path fails; this pattern copies
# a placeholder only when ui/dist exists in the build context).
# NOTE: uncomment once the UI build is wired into CI:
# COPY ui/dist /app/ui/dist

# Bundles live on a mounted volume in prod; default matches config BUNDLE_DIR.
RUN mkdir -p /data/bundles && chown -R stoker:stoker /data

EXPOSE 8080
USER stoker

# uvicorn entry: server/app.py exposes `app = create_app()`.
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
