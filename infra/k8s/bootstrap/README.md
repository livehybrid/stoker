# Declarative Stoker bootstrap (EKS / SOK)

Bring a fresh Stoker control plane up **fully configured and already generating**
as part of the Terraform build, with no manual UI step. It removes the two
hand-steps a fresh install otherwise needs: creating the admin, and registering
packs / targets / specs / runs.

## Why a Kubernetes Job (not the Terraform `http`/`restapi` provider, not `local-exec`)

Stoker is reached at the in-cluster ClusterIP service `stoker:8080`. On a private
EKS estate the Terraform runner usually cannot reach that service, and the
existing `splunk-egress-isolation` NetworkPolicy already permits exactly what a
Job needs (DNS, the ClusterIP CIDR to reach Stoker and Splunk HEC by service
name, and `:443` out for git pack sync). A Job therefore:

- runs where the service actually resolves,
- keeps the HEC token and admin password in a Kubernetes Secret, never in
  Terraform state or output,
- lets `terraform apply` **block until Stoker is bootstrapped and running**
  (`wait_for_completion = true`), or fail loudly.

## Flow

```mermaid
sequenceDiagram
    participant TF as terraform apply
    participant K8s as EKS
    participant CP as Stoker CP (stoker:8080)
    participant Job as bootstrap Job
    participant GH as github.com
    participant HEC as Splunk HEC

    TF->>K8s: apply Deployment + Service (stoker.tf)
    TF->>K8s: apply Secret + ConfigMap + Job (stoker-bootstrap.tf)
    K8s->>Job: start (image = CP image, cmd = python3 bootstrap.py)
    Job->>CP: GET /healthz (poll until ready)
    Job->>CP: GET /api/auth/status
    alt no admin yet
        Job->>CP: POST /api/auth/setup (win the first-visit race)
    end
    Job->>CP: POST /api/auth/login  (session cookie)
    Job->>CP: POST /api/targets      (HEC token from Secret env)
    Job->>CP: POST /api/repos + /sync
    CP->>GH: git clone/fetch sample packs
    Job->>CP: POST /api/specs
    Job->>CP: POST /api/specs/{id}/run
    CP->>K8s: create Indexed Job (worker fleet, k8s-local)
    K8s->>HEC: workers deliver events
    Job-->>TF: exit 0  (apply returns; runs are live)
```

## Files

| File | Purpose |
|------|---------|
| `bootstrap.py` | Stdlib-only reconciler. Idempotent: create-if-absent by natural key; a run starts only when its spec has no active run. |
| `desired-state.example.json` | The declarative document: targets, pack repos, specs, and which specs `run`. Copy/edit, or drive it from Terraform (see below). |
| `stoker-bootstrap.tf` | Drop-in for the `sok` layer: Secret + ConfigMap + Job. Reuses `local.namespace`, `local.stoker_labels`, `var.sok_stoker_image` from `stoker.tf`. |

## Use it from Terraform

1. Put `stoker-bootstrap.tf` and `bootstrap.py` in the same layer as `stoker.tf`
   (`terraform/layers/sok/`).
2. Set the variables (in your `vars/*.tfvars` or wherever `sok_*` live):

   ```hcl
   sok_stoker_bootstrap           = true
   sok_stoker_bootstrap_hec_url   = "https://splunk-hec.splunk.svc.cluster.local:8088"
   sok_stoker_bootstrap_hec_token = data.aws_secretsmanager_secret_version.hec_token.secret_string  # not a literal
   sok_stoker_bootstrap_index     = "loadtest"
   ```

3. Edit `local.stoker_bootstrap_desired` in `stoker-bootstrap.tf` to declare the
   specs and runs you want. `terraform apply` then leaves Stoker configured and
   the flagged specs running. Re-applying is safe (idempotent); changing the
   script or desired state re-runs the Job (its name carries a content hash).

The admin username is `stoker-admin`; the password is derived deterministically
from `pass4SymmKey` (same pattern as the master key — stable, no extra secret).
Retrieve it when a human needs the UI:

```
kubectl -n <ns> get secret stoker-bootstrap -o jsonpath='{.data.STOKER_ADMIN_PASSWORD}' | base64 -d
```

## Test it without Terraform (plain kubectl)

```bash
NS=splunk
kubectl -n $NS create secret generic stoker-bootstrap \
  --from-literal=STOKER_ADMIN_USER=stoker-admin \
  --from-literal=STOKER_ADMIN_PASSWORD="$(openssl rand -hex 16)" \
  --from-literal=STOKER_TARGET_HEC_TOKEN="$HEC_TOKEN"
kubectl -n $NS create configmap stoker-bootstrap \
  --from-file=bootstrap.py \
  --from-file=desired-state.json=desired-state.example.json
kubectl -n $NS create job stoker-bootstrap --image="$STOKER_IMAGE" -- \
  python3 /bootstrap/bootstrap.py
# (mount the configmap at /bootstrap and env_from the secret — see stoker-bootstrap.tf
#  for the exact pod spec; the Job manifest is the source of truth.)
kubectl -n $NS logs job/stoker-bootstrap -f
```

## Notes & gotchas

- **Packs need egress or a local copy.** The images ship no `packs/` tree. The
  default desired state git-syncs `livehybrid/stoker-sample-packs`, which needs
  `:443` to github.com (the SOK NetworkPolicy allows it). With no internet
  egress, copy packs onto the PVC (`kubectl cp .../packs/<name>
  <pod>:/data/packs/<name>`) and register them with a `source_path` pack instead
  of a repo — the reconciler's repo step is then simply omitted.
- **Self-signed HEC**: `verify_tls: false` on the target. The control plane also
  projects `STOKER_HEC_VERIFY_TLS=0` to the workers for that target (upstream
  `ae34bab`), so the whole path trusts the self-signed cert.
- **Fleet** is `k8s-local` — the auto-seeded fleet whose namespace and node
  selector come from `K8S_NAMESPACE` / `K8S_NODE_SELECTOR` on the Deployment.
- **Long runs**: a per-run JWT lasts 1 h by default. For runs meant to generate
  for longer, set `STOKER_JWT_TTL_S` on the control-plane Deployment (rolling
  heartbeats refresh the token, so this mainly matters if workers churn).
- **Per-worker rate ceilings** default to the built-in table; raise them with
  `STOKER_MAX_EPS_PER_WORKER` / `STOKER_MAX_GB_DAY_PER_WORKER` (or the per-engine
  variants) on the Deployment if a spec needs a higher share.
