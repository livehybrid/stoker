# Input variables for the Stoker EKS module.
#
# No secret has a default. The house egress IP, the Budgets email and the
# cluster name are required inputs; everything else defaults to the DESIGN.md
# section 11 "AWS" ground truth (eu-west-2, namespace "stoker", arm64 Graviton
# spot workers). Secrets and site-specific values are fed from SOPS-decrypted
# tfvars at apply time (see README), never committed.

# --------------------------------------------------------------------------- #
# Identity / region
# --------------------------------------------------------------------------- #

variable "region" {
  description = "AWS region. The design fixes eu-west-2 (London)."
  type        = string
  default     = "eu-west-2"
}

variable "cluster_name" {
  description = "EKS cluster name (also the resource name prefix)."
  type        = string
  default     = "stoker"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,38}$", var.cluster_name))
    error_message = "cluster_name must be lower-case alphanumeric/hyphen, 2-39 chars, starting with a letter."
  }
}

variable "kubernetes_version" {
  description = <<-EOT
    EKS control-plane Kubernetes version. Must be >= 1.27 for Elastic Indexed
    Jobs (the K8sDriver scales by patching parallelism+completions together).
    Pin explicitly and bump consciously to dodge the extended-support fee.
  EOT
  type        = string
  default     = "1.30"

  validation {
    # Reject anything below 1.27 outright: the driver's scale() would silently
    # break on an older control plane.
    condition     = tonumber(split(".", var.kubernetes_version)[1]) >= 27
    error_message = "kubernetes_version must be >= 1.27 (Elastic Indexed Jobs)."
  }
}

variable "tags" {
  description = "Tags applied to every taggable resource (cost allocation, ownership)."
  type        = map(string)
  default = {
    Project   = "stoker"
    ManagedBy = "terraform"
    Lifecycle = "cattle" # destroyed when idle; see README
  }
}

# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #

variable "vpc_cidr" {
  description = "CIDR for the module-created VPC."
  type        = string
  default     = "10.60.0.0/16"
}

variable "public_subnet_cidrs" {
  description = <<-EOT
    Public subnet CIDRs (one per AZ). Workers run here with public IPs and
    egress-only security groups so NAT Gateway data processing ($0.045/GB) stays
    out of the data path entirely (design section 11 + the egress gate).
  EOT
  type        = list(string)
  default     = ["10.60.0.0/20", "10.60.16.0/20", "10.60.32.0/20"]
}

variable "azs" {
  description = "Availability zones to spread subnets across (must match public_subnet_cidrs length)."
  type        = list(string)
  default     = ["eu-west-2a", "eu-west-2b", "eu-west-2c"]
}

variable "house_egress_cidr" {
  description = <<-EOT
    The house public egress IP in CIDR form (e.g. "203.0.113.4/32"). REQUIRED,
    no default: it restricts the EKS *public* API endpoint access list to only
    the on-prem control plane, and seeds the (deny-by-default) worker egress and
    control-plane admin allowances. A "/0" here is rejected.
  EOT
  type        = string

  validation {
    condition     = can(cidrhost(var.house_egress_cidr, 0)) && var.house_egress_cidr != "0.0.0.0/0"
    error_message = "house_egress_cidr must be a valid CIDR and must not be 0.0.0.0/0 (the public API must not be world-open)."
  }
}

variable "extra_api_access_cidrs" {
  description = "Additional CIDRs allowed to reach the public API endpoint (e.g. an operator's static IP). Empty by default."
  type        = list(string)
  default     = []
}

# --------------------------------------------------------------------------- #
# Worker NodePool (Karpenter, Graviton spot)
# --------------------------------------------------------------------------- #

variable "worker_arch" {
  description = "Worker CPU architecture. Graviton (arm64) is the design default (cheaper spot, matches the multi-arch worker image)."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "amd64"], var.worker_arch)
    error_message = "worker_arch must be arm64 or amd64."
  }
}

variable "worker_instance_families" {
  description = "Instance families Karpenter may provision for workers (Graviton compute-optimised by default)."
  type        = list(string)
  default     = ["c7g", "c6g", "m7g", "m6g"]
}

variable "worker_capacity_type" {
  description = "Karpenter capacity type for workers. Spot: workers are stateless restartable cattle (leases re-issue on reclaim)."
  type        = list(string)
  default     = ["spot"]
}

variable "worker_nodepool_cpu_limit" {
  description = <<-EOT
    Hard ceiling on total vCPUs Karpenter may provision for the worker NodePool.
    A spend backstop in addition to the Budgets alarm: caps how big a forgotten
    campaign can scale. 500k EPS needs ~500 vCPU (design section 12), so 1000 is
    generous headroom while still bounded.
  EOT
  type        = number
  default     = 1000
}

variable "karpenter_version" {
  description = "Karpenter Helm chart version (pin consciously; Karpenter's CRDs move)."
  type        = string
  default     = "1.0.6"
}

# --------------------------------------------------------------------------- #
# Control-plane access principal
# --------------------------------------------------------------------------- #

variable "control_plane_principal_name" {
  description = "IAM user name for the on-prem control plane (its only permission is eks:DescribeCluster; cluster access comes via an EKS access entry)."
  type        = string
  default     = "stoker-control-plane"
}

variable "create_control_plane_access_key" {
  description = <<-EOT
    Whether to mint an IAM access key for the control-plane user in Terraform.
    The secret is a sensitive output (consume via SOPS into fleets.config_json,
    then rotate per the README). Set false to attach a key out-of-band instead.
  EOT
  type        = bool
  default     = true
}

# --------------------------------------------------------------------------- #
# Budgets alarm (NON-OPTIONAL per the design)
# --------------------------------------------------------------------------- #

variable "budget_limit_usd" {
  description = "Monthly cost budget (USD) for the Budgets alarm. The $73/month idle control plane dominates light use; alarm well below the extended-support trap."
  type        = number
  default     = 150
}

variable "budget_alert_email" {
  description = "Email address the Budgets SNS alarm notifies. REQUIRED, no default: the design makes the budget alarm non-optional."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be a valid email address."
  }
}

variable "budget_alert_thresholds_pct" {
  description = "Percent-of-budget thresholds that fire an alert (actual + forecast)."
  type        = list(number)
  default     = [50, 80, 100]
}
