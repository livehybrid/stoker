# Stoker

Central web UI and control plane for orchestrating fleets of Splunk HEC data generators. Configure a job (sample pack from git, target, rate, worker count), press start and Stoker launches disposable worker containers on Docker Swarm or Kubernetes (k3s, EKS) that generate eventgen-compatible sample data and deliver it over HEC at an exact aggregate rate.

Status: **Phase 0** — the worker image (`ghcr.io/livehybrid/stoker-worker`): vendored eventgen engine, pacing agent and HEC client. Control plane and UI follow in Phase 1.

## Layout

```
worker/    agent (pacing, HEC client, control-plane protocol) + vendored engines
server/    FastAPI control plane (Phase 1)
ui/        React UI (Phase 1)
packs/     example sample packs
infra/     swarm stack, k8s manifests, Terraform EKS (Phases 1-3)
docs/      WORKER-CONTRACT.md and design references
```

## Worker quick start (standalone, no control plane)

```bash
docker run --rm \
  -e STOKER_STANDALONE=1 \
  -e STOKER_BUNDLE=/packs/flatline \
  -e STOKER_HEC_URL=http://splunk:8088 \
  -e STOKER_HEC_TOKEN=<token> \
  -e STOKER_INDEX=loadtest \
  -e STOKER_RATE_MODE=eps -e STOKER_RATE_VALUE=100 \
  -e STOKER_DURATION_S=120 \
  -v $(pwd)/packs:/packs:ro \
  ghcr.io/livehybrid/stoker-worker:latest
```

See `docs/WORKER-CONTRACT.md` for the full worker contract.

## Licence

Apache-2.0. Vendors [splunk/eventgen](https://github.com/splunk/eventgen) 7.2.1 (Apache-2.0); see `worker/engines/eventgen/VENDOR.md`.
