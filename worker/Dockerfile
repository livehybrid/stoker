# syntax=docker/dockerfile:1
# Build from the repo root: docker buildx build -f worker/Dockerfile -t stoker-worker .
FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin stoker

WORKDIR /app

# Dependency layer first so source-only changes reuse the pip layer.
COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY worker/stoker_agent /app/stoker_agent
COPY worker/engines /app/engines

# /app for the agent package, engines/eventgen for the vendored splunk_eventgen
# tree, engines/rawreplay for the PISTON raw-replay engine (`-m stoker_rawreplay`).
ENV PYTHONPATH=/app:/app/engines/eventgen:/app/engines/rawreplay

USER stoker

ENTRYPOINT ["python", "-m", "stoker_agent"]
