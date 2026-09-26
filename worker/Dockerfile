# syntax=docker/dockerfile:1
# Build from the repo root: docker buildx build -f worker/Dockerfile -t stoker-worker .

# ---- firebox: the Rust eventgen engine (git submodule at worker/engines/firebox)
# Cross-compiled natively on the build host for the requested platform (no
# emulation; the musl targets link with rustc's bundled lld). When the
# submodule is not checked out the stage produces nothing and the agent falls
# back to the vendored Python eventgen at runtime (STOKER_EVENTGEN_IMPL=auto).
FROM --platform=$BUILDPLATFORM rust:1-bookworm AS firebox
ARG TARGETPLATFORM
WORKDIR /src
COPY worker/engines/firebox/ /src/
# sharing=locked: the amd64 and arm64 stages build in parallel and would
# otherwise unpack the same crates into the shared registry at once.
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/src/target,id=firebox-target-$TARGETPLATFORM \
    set -eu; mkdir -p /out; \
    if [ ! -f /src/Cargo.toml ]; then \
        echo "worker/engines/firebox is empty (submodule not initialised): the image will run the Python eventgen"; \
        exit 0; \
    fi; \
    case "$TARGETPLATFORM" in \
        linux/arm64) T=aarch64-unknown-linux-musl ;; \
        *)           T=x86_64-unknown-linux-musl ;; \
    esac; \
    rustup target add "$T"; \
    cargo build --release --locked --target "$T" --no-default-features --features http; \
    cp "target/$T/release/firebox" /out/firebox; \
    /out/firebox version || true

FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin stoker

WORKDIR /app

# Dependency layer first so source-only changes reuse the pip layer.
COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY worker/stoker_agent /app/stoker_agent
COPY worker/engines/eventgen /app/engines/eventgen
COPY worker/engines/rawreplay /app/engines/rawreplay
COPY worker/engines/metrics /app/engines/metrics

# The firebox engine binary (absent when the submodule was not checked out).
COPY --from=firebox /out/ /usr/local/bin/

# /app for the agent package, engines/eventgen for the vendored splunk_eventgen
# tree, engines/rawreplay for the PISTON raw-replay engine (`-m stoker_rawreplay`).
ENV PYTHONPATH=/app:/app/engines/eventgen:/app/engines/rawreplay

USER stoker

ENTRYPOINT ["python", "-m", "stoker_agent"]
