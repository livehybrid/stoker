# Stoker EKS module (`infra/aws/stoker-eks`)

Terraform for the Stoker **`eks`** fleet: an EKS cluster in **eu-west-2** whose
worker nodes are egress-only Graviton spot cattle, driven by the on-prem Stoker
control plane through the same six-method `ExecutionDriver` (`K8sDriver`) that
serves local k3s. This is the AWS half of DESIGN.md section 11.

> **Status: UNAPPLIED.** This module has never been `terraform apply`-ed from
> this repo checkout (no AWS credentials in the build environment; `terraform
> init/plan/validate` have not been run here). The HCL is written to be
> internally consistent and secure, but the community-module wiring and the
> Karpenter CRD manifests are **skeleton-quality** until a real `terraform plan`
> runs against an AWS account. Read the "Two-phase apply" and "Known caveats"
> sections before first use.

## What it builds

| Area | Resource | Design requirement |
|---|---|---|
| Cluster | `terraform-aws-modules/eks/aws` (pinned `~> 20.24`), `authentication_mode = "API"` | EKS, not ECS: one k8s driver serves k3s + EKS |
| API endpoint | public access **restricted to `house_egress_cidr`** (+ `extra_api_access_cidrs`); private access on | "public endpoint access CIDR restricted to the house egress IP" |
| Secrets at rest | KMS CMK (`aws_kms_key.eks`), `cluster_encryption_config` on `secrets` | "EKS secrets envelope-encrypted with a KMS key" |
| Workers | Karpenter NodePool: **arm64 Graviton**, **spot**, public subnets, public IPs, `stoker.io/worker` taint, hard vCPU limit | "Karpenter with a Graviton spot NodePool"; "public subnets with public IPs" |
| Worker SG | `aws_security_group.worker_egress_only`: egress rule only, **no ingress rule at all** | "security groups blocking all ingress: workers are egress-only cattle" |
| No NAT | VPC has `enable_nat_gateway = false` | "keeps NAT Gateway data-processing out of the data path" |
| Namespace | `stoker` | "a namespace `stoker`" |
| RBAC | `Role` limited to `jobs`/`pods`/`pods/log`/`secrets` (create/list/watch/delete etc.), bound to group `stoker:control-plane` | "a namespaced Role limited to ... create, list, watch, delete (NOT cluster-admin)" |
| NetworkPolicy | `stoker-workers-egress-only` (deny ingress; egress DNS + 443/8088) | "a NetworkPolicy makes workers egress-only" |
| Control-plane IAM | `aws_iam_user.control_plane` with **only** `eks:DescribeCluster` (on this cluster ARN), mapped via `aws_eks_access_entry` to `stoker:control-plane` | "a dedicated IAM principal ... whose only permission is eks:DescribeCluster, mapped via an EKS access entry to the namespaced Role" |
| Cost | `aws_budgets_budget` + SNS email (`budget_alert_email`) | "a NON-OPTIONAL AWS Budgets alarm (SNS to an email var)" |

Everything sensitive or site-specific is a **variable**; nothing is hardcoded.
Only two variables are required (no default): `house_egress_cidr` and
`budget_alert_email`.

## Files

```
versions.tf              provider + terraform version pins
variables.tf             all inputs (2 required, rest defaulted to the design)
main.tf                  providers, KMS, VPC (public-only), EKS, worker egress-only SG
karpenter.tf             Karpenter IAM/Helm + EC2NodeClass + Graviton spot NodePool
rbac.tf                  stoker namespace, least-priv Role/binding/SA, NetworkPolicy
iam_control_plane.tf     DescribeCluster-only IAM user + access key + EKS access entry
budgets.tf               non-optional Budgets alarm -> SNS email
outputs.tf               cluster identity + (sensitive) control-plane key + exec kubeconfig
terraform.tfvars.example copy to a SOPS-encrypted terraform.tfvars
```

## Prerequisites

- Terraform >= 1.6, the AWS CLI v2 (the kubeconfig uses `aws eks get-token`
  exec auth), and AWS credentials for an admin/bootstrap principal (only for the
  `apply`; the control plane itself uses the tiny `eks:DescribeCluster` user).
- The tfvars supplied via SOPS (the house pattern, DESIGN.md section 14: "The
  same SOPS flow feeds Terraform tfvars for AWS"), never a plaintext committed
  file.

## State

The `backend "s3"` block in `versions.tf` is commented out, so state is **local**
by default (fine for the skeleton, wrong for shared real use). Before the first
real apply, wire a remote backend (S3 bucket + DynamoDB lock table in
eu-west-2) so the on-prem control plane and any operator share one state and
never race a destroy.

## Apply / destroy — cluster as cattle

Confirmed 2026-07-11: **the cluster is cattle, `terraform destroy` whenever it
is not in use.** All durable state lives in the on-prem Postgres and
`/mnt/aios`; the ~$73/month control-plane fee dominates light use, and leaving
it up risks the ~$438/month extended-support trap on an ageing version.

Start a campaign:

```bash
# 1. decrypt tfvars from SOPS to a tempfile (never commit the plaintext)
sops -d terraform.tfvars.enc > /tmp/stoker-eks.tfvars

# 2. bring the cluster up (~15-20 min to a ready NodePool)
terraform init
terraform apply -var-file=/tmp/stoker-eks.tfvars      # two-phase: see below

# 3. register the fleet with the control plane (outputs feed fleets.config_json)
terraform output -raw cluster_name
terraform output -raw control_plane_access_key_id
terraform output -raw control_plane_secret_access_key   # sensitive -> SOPS

shred -u /tmp/stoker-eks.tfvars
```

Tear it down when the campaign ends:

```bash
terraform destroy -var-file=/tmp/stoker-eks.tfvars
```

Because destroy is a manual convention, the Stoker admin screen surfaces the
cluster version age and the idle-fleet warning (DESIGN.md section 10), and the
Budgets alarm backstops a forgotten cluster.

### Two-phase apply (Karpenter CRDs)

The `kubernetes_manifest` resources in `karpenter.tf` and the NetworkPolicy in
`rbac.tf` are validated by the provider against the cluster's live CRD/OpenAPI
schema **at plan time**, which does not exist until the cluster and the
Karpenter controller are up. On a clean apply, target the cluster + controller
first, then the CRD objects:

```bash
# phase 1: cluster, KMS, VPC, EKS, Karpenter controller, IAM, budgets, namespace
terraform apply -var-file=... \
  -target=module.vpc -target=module.eks -target=module.karpenter \
  -target=helm_release.karpenter -target=kubernetes_namespace.stoker

# phase 2: the NodePool/EC2NodeClass/NetworkPolicy + everything else
terraform apply -var-file=...
```

Subsequent applies are single-phase (the CRDs already exist).

## The egress gate (hard, not advisory)

AWS internet egress is **~$0.09/GB** after the first 100 GB/month free. A single
10k EPS x 500 B job is ~13 TB/month ≈ **$1,170/month** of transfer — more than
the compute. With a NAT gateway in the path it would be ~$1,750/month
(+$0.045/GB data processing), which is exactly why this module puts workers in
**public subnets with public IPs and no NAT** (`enable_nat_gateway = false`).
**If that topology ever changes, `/estimate` must add the NAT term or the hard
gate under-quotes by ~35%.**

Therefore, non-negotiably:

1. **gzip is mandatory** in the HEC client (built in, not a toggle): 5-10x on
   text logs.
2. `/estimate` computes projected monthly egress for any `eks`-fleet spec
   against a non-AWS endpoint; high-volume soaks default to the **local** fleet
   against `192.168.0.222`, where egress is free.
3. An `eks`-fleet run against a **Splunk Cloud target without PrivateLink**
   requires a **typed admin override** at submit time (not a dismissible
   warning). **PrivateLink** (~$0.01/GB, roughly 9x cheaper, enabled via ACS on
   eligible stacks) is the recommended path; as of 2026-07-11 no PrivateLink-
   eligible stack exists, so the typed override is the expected path for cloud
   targets initially.

This module does not create any PrivateLink/VPC-endpoint resources: no AWS
ingress, no ALB, nothing public in AWS beyond the restricted API endpoint. The
control plane stays on-prem and drives EKS outbound only.

## Control-plane access-key rotation (exact steps)

The control-plane IAM user's access key lives **Fernet-encrypted in
`fleets.config_json`** for fleet `eks` (like every other credential). Rotate
with the zero-downtime create-then-delete dance:

1. **Create** a second access key for the user (AWS allows two at once):
   ```bash
   aws iam create-access-key --user-name "$(terraform output -raw control_plane_principal_arn | awk -F/ '{print $NF}')"
   ```
   (or `terraform taint aws_iam_access_key.control_plane[0] && terraform apply`
   to have Terraform mint the replacement.)
2. **Update the fleet record**: write the new `AccessKeyId` / `SecretAccessKey`
   into fleet `eks`'s `config_json` through the Stoker API (write-only, Fernet-
   encrypted). The control plane picks it up on the next `aws eks get-token`.
3. **Verify** a `status`/`DescribeCluster` succeeds with the new key (start a
   trivial run or hit the fleet health probe).
4. **Delete** the old key:
   ```bash
   aws iam delete-access-key --user-name <user> --access-key-id <OLD_KEY_ID>
   ```

The key only ever grants `eks:DescribeCluster` on this one cluster ARN; all
cluster power is the namespaced RBAC via the access entry, so a leaked key
cannot escalate beyond create/list/watch/delete of Jobs/Pods/Secrets in the
`stoker` namespace. Still rotate on the normal cadence and immediately on any
suspected control-plane compromise (that host also holds the Fernet master key).

## How the control plane authenticates (exec auth)

The `K8sDriver` builds its client from a kubeconfig context registered as fleet
`eks`. That kubeconfig (see the `exec_kubeconfig` output) uses **exec auth**:

```yaml
users:
- name: stoker-control-plane
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args: [eks, get-token, --cluster-name, stoker, --region, eu-west-2]
```

`aws eks get-token` runs with the control-plane user's `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` (injected from the Fernet-decrypted `fleets.config_json`
at exec time — never written to disk by Terraform), mints a short-lived token,
and the token maps to the `stoker:control-plane` RBAC group via the access
entry. No long-lived kubeconfig token, no aws-auth ConfigMap.

## Known caveats (skeleton honesty)

- **Unapplied**: no `terraform validate/plan` has run here. Expect to reconcile
  minor argument names against the exact pinned module versions on first real
  plan (community modules rename inputs between minors).
- **Karpenter version**: `karpenter_version` / the `v1` CRD API versions
  (`karpenter.sh/v1`, `karpenter.k8s.aws/v1`) are pinned to the 1.x line;
  confirm they match the chart you pin before applying.
- **NetworkPolicy enforcement** needs a CNI that enforces it — enable the EKS
  VPC CNI network-policy add-on (or run Cilium). Without it the manifest applies
  but does not restrict traffic; the worker **security group** (no ingress)
  still holds at the node level.
- **Budget email** must be confirmed once (AWS emails a subscription-confirm
  link on first apply).
- **Cost sanity**: EKS control plane ~$73/month; workers are spot Graviton
  (blended 59-77% savings); the dominant risk is **egress**, gated above.
