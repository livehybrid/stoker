# Stoker Kubernetes reference manifests (`infra/k8s`)

Static YAML that documents the shapes the `K8sDriver` works with, and bootstraps
a cluster's **RBAC + NetworkPolicy** so it is ready to host Stoker runs. Two
audiences:

1. **k3s (local)** — there is no Terraform for k3s (DESIGN.md section 11: local
   k3s is an option the K8sDriver unlocks "for free"). Apply `rbac.yaml` +
   `networkpolicy.yaml` once to prepare a k3s cluster. On **EKS** the same
   objects are created by `infra/aws/stoker-eks` (Terraform), so you do **not**
   apply these there.
2. **Reference** — `job-template.yaml` is the exact per-run Secret + Indexed-Job
   shape the driver builds at runtime (`server/drivers/k8s.py::_job_manifest`).
   It is annotated with a `<PLACEHOLDER>` legend and is never applied by the
   control plane (the driver builds it in code); keep the two in sync when
   either changes.

## Files

```
rbac.yaml           namespace SA + least-privilege Role (jobs/pods/pods-log/secrets) + binding
networkpolicy.yaml  workers egress-only (deny ingress; egress DNS + HEC + control plane; IMDS denied)
job-template.yaml   reference per-run Secret + Indexed Job (what the driver builds)
```

> The `stoker` namespace itself is assumed to exist (create it with
> `kubectl create namespace stoker`, or let `infra/aws/stoker-eks` create it on
> EKS). `rbac.yaml`/`networkpolicy.yaml` are namespaced into `stoker`.

## Apply (k3s only)

```bash
# k3s must be installed with the bundled Traefik + ServiceLB DISABLED so nothing
# contends with the swarm Traefik that owns :80/:443 on the same nodes, and with
# secrets encryption on (the k3s equivalent of the EKS KMS envelope):
#   curl -sfL https://get.k3s.io | sh -s - \
#     --disable traefik --disable servicelb --secrets-encryption

kubectl create namespace stoker
kubectl apply -f rbac.yaml
# edit networkpolicy.yaml first: replace <HEC_TARGET_CIDR> / <CONTROL_PLANE_CIDR>
kubectl apply -f networkpolicy.yaml
```

Then register the k3s context as a Stoker fleet (`fleets.config_json`:
`{"kube_context": "<ctx>", "namespace": "stoker"}`, driver `k8s`).

> NetworkPolicy enforcement needs a CNI that enforces it. k3s ships flannel,
> which does **not** enforce NetworkPolicy by default; install one that does
> (e.g. k3s `--flannel-backend=none` + Calico, or Cilium) or accept that on k3s
> the policy is documentary. The design accepts LAN-reachable swarm workers as
> the parity posture (DESIGN.md section 14).

## Consistency with the EKS Terraform

`rbac.yaml` and `networkpolicy.yaml` are the k3s twins of the RBAC/NetworkPolicy
in `infra/aws/stoker-eks/rbac.tf`. Both bind the same least-privilege Role to:

- the RBAC **group `stoker:control-plane`** — on EKS the access entry maps the
  `eks:DescribeCluster`-only IAM principal into this group (the live EKS path);
- the **ServiceAccount `stoker-driver`** — for k3s / a control plane that
  authenticates as the SA directly.

Keep the verb set (`jobs`/`pods`/`pods/log`/`secrets`) identical across the two
when it changes.

## What the driver creates per run (not applied here)

Per run the driver creates, in the `stoker` namespace:

- a per-run **Secret** `stoker-run-<id>-hec` carrying the HEC token in
  `stringData` (never inlined into the pod env), with an `ownerReference` on the
  Job so it is garbage-collected with it;
- one **Indexed Job** `stoker-run-<id>` (`job-template.yaml` shape).

Both are labelled `stoker.run=<id>` for boot reconciliation. See
`docs/WORKER-CONTRACT.md` for the worker env the Job injects, and
`server/drivers/k8s.py` for the authoritative construction.
