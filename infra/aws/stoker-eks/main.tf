# Stoker EKS module — main resources.
#
# DESIGN.md section 11 "AWS" is the ground truth. Topology in one breath:
#
#   * an EKS cluster (secrets envelope-encrypted with a KMS key), its PUBLIC API
#     endpoint access list restricted to the house egress IP;
#   * Karpenter with a Graviton (arm64) SPOT NodePool for workers;
#   * worker nodes in PUBLIC subnets with public IPs and a security group that
#     blocks ALL ingress (egress-only cattle — keeps NAT data-processing out of
#     the data path);
#   * a "stoker" namespace + a namespaced Role (jobs/pods/secrets:
#     create/list/watch/delete) bound to a ServiceAccount (least privilege, NOT
#     cluster-admin);
#   * a dedicated IAM principal for the control plane whose ONLY permission is
#     eks:DescribeCluster, mapped via an EKS access entry to that namespaced
#     Role (exec-auth: `aws eks get-token`, documented in the README);
#   * a NON-OPTIONAL AWS Budgets alarm (SNS to an email var).
#
# UNAPPLIED: no AWS credentials in this environment; `terraform init/plan` has
# not been run here. The HCL is written to be internally consistent and secure,
# but treat the community-module wiring as skeleton-quality until a real
# `terraform plan` is run against an AWS account (see README).

# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

provider "aws" {
  region = var.region
  default_tags {
    tags = var.tags
  }
}

# The kubernetes/helm providers authenticate to the freshly-created cluster with
# a short-lived token (exec plugin), the same mechanism the on-prem control
# plane uses. No kubeconfig file is written to disk by Terraform.
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
    }
  }
}

# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  namespace       = "stoker"
  worker_arch_k8s = var.worker_arch # "arm64" | "amd64"; matches kubernetes.io/arch

  # The API endpoint public-access allow-list: always the house egress IP, plus
  # any explicit operator CIDRs. Never 0.0.0.0/0 (variable validation enforces).
  api_access_cidrs = distinct(concat([var.house_egress_cidr], var.extra_api_access_cidrs))

  tags = var.tags
}

# --------------------------------------------------------------------------- #
# KMS key for EKS secrets envelope encryption (design: "EKS secrets
# envelope-encrypted with a KMS key")
# --------------------------------------------------------------------------- #

resource "aws_kms_key" "eks" {
  description             = "${var.cluster_name} EKS secrets envelope encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = local.tags
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${var.cluster_name}-eks-secrets"
  target_key_id = aws_kms_key.eks.key_id
}

# --------------------------------------------------------------------------- #
# VPC — public subnets only (workers are egress-only cattle in public subnets;
# no NAT gateway, so NAT data-processing never touches the HEC data path).
# --------------------------------------------------------------------------- #

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.8"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs            = var.azs
  public_subnets = var.public_subnet_cidrs

  # No private subnets and NO NAT gateway on purpose: every worker sits in a
  # public subnet with a public IP (design section 11). This removes the
  # $0.045/GB NAT data-processing term from a data path that can push terabytes.
  enable_nat_gateway = false
  single_nat_gateway = false

  # Public IPs for pods' nodes; DNS for HEC/control-plane name resolution.
  map_public_ip_on_launch = true
  enable_dns_hostnames    = true
  enable_dns_support      = true

  # Tags Karpenter/EKS use to discover subnets for node placement.
  public_subnet_tags = {
    "kubernetes.io/role/elb"                    = "1"
    "karpenter.sh/discovery"                    = var.cluster_name
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }

  tags = local.tags
}

# --------------------------------------------------------------------------- #
# EKS cluster (community module, pinned). Public endpoint restricted; secrets
# KMS-encrypted; access-entry authentication mode.
# --------------------------------------------------------------------------- #

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name    = var.cluster_name
  cluster_version = var.kubernetes_version

  # Public endpoint ON but access-restricted to the house egress IP (design:
  # "The EKS public endpoint access CIDR is restricted to the house egress IP").
  # Private access ON too so in-cluster components (Karpenter) reach the API
  # without traversing the internet.
  cluster_endpoint_public_access       = true
  cluster_endpoint_public_access_cidrs = local.api_access_cidrs
  cluster_endpoint_private_access      = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  # Envelope-encrypt Kubernetes Secrets at rest with our CMK.
  cluster_encryption_config = {
    provider_key_arn = aws_kms_key.eks.arn
    resources        = ["secrets"]
  }

  # Access entries are the modern (aws-auth ConfigMap-free) auth path; the
  # control-plane principal is granted access this way (below), scoped to the
  # namespaced Role via RBAC, never cluster-admin.
  authentication_mode = "API"

  # The Terraform caller bootstraps cluster RBAC (creates the namespace, Role,
  # binding). Grant it admin *for bootstrap only*; the control-plane principal
  # gets the least-privilege entry.
  enable_cluster_creator_admin_permissions = true

  # A minimal managed node group hosts only the Karpenter controller + coredns;
  # all *worker* capacity comes from the Karpenter NodePool (spot Graviton).
  # Karpenter should not schedule onto the nodes it runs on, hence a tiny fixed
  # group here dedicated to system pods.
  eks_managed_node_groups = {
    system = {
      ami_type       = "AL2023_ARM_64_STANDARD"
      instance_types = ["t4g.medium"]
      capacity_type  = "ON_DEMAND"

      min_size     = 2
      max_size     = 3
      desired_size = 2

      labels = {
        "stoker.io/role" = "system"
      }
      tags = merge(local.tags, { "stoker.io/nodegroup" = "system" })
    }
  }

  # Discovery tag so Karpenter finds the cluster security group.
  node_security_group_tags = merge(local.tags, {
    "karpenter.sh/discovery" = var.cluster_name
  })

  tags = local.tags
}

# --------------------------------------------------------------------------- #
# Worker egress-only security group (blocks ALL ingress; allows egress).
# Karpenter attaches this to worker nodes via the NodeClass (see karpenter.tf).
# --------------------------------------------------------------------------- #

resource "aws_security_group" "worker_egress_only" {
  name_prefix = "${var.cluster_name}-worker-egress-"
  description = "Stoker workers: egress-only cattle. NO ingress at all."
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.tags, {
    Name                     = "${var.cluster_name}-worker-egress-only"
    "karpenter.sh/discovery" = var.cluster_name
  })

  lifecycle {
    create_before_destroy = true
  }
}

# Egress: allow all outbound (workers must reach the house HEC + control plane
# over the internet; the design accepts internet egress and mandates gzip to
# bound its cost). The *ingress* silence is the security property: with no
# ingress rule, the default-deny SG blocks every inbound connection.
resource "aws_security_group_rule" "worker_egress_all" {
  security_group_id = aws_security_group.worker_egress_only.id
  type              = "egress"
  description       = "Egress-only: HEC + control plane over the internet, DNS, etc."
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  cidr_blocks       = ["0.0.0.0/0"]
}

# NOTE: deliberately NO aws_security_group_rule of type "ingress" exists for
# this SG. That absence is the control: AWS security groups deny all ingress by
# default, so workers accept no inbound connections (egress-only cattle).
