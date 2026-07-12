# Karpenter — Graviton (arm64) SPOT NodePool for Stoker workers.
#
# Workers are ideal spot tenants (design section 11): stateless, restartable,
# leases re-issued on replacement. Karpenter provisions nodes just-in-time into
# the PUBLIC subnets, tagged so the egress-only worker SG attaches, with a hard
# vCPU ceiling as a spend backstop.
#
# UNAPPLIED: the EC2NodeClass / NodePool are applied as Kubernetes CRDs via the
# kubernetes_manifest resource, which requires a LIVE cluster at plan time (the
# provider validates against the CRD openAPI schema). On a real apply, run this
# in two phases (see README "Two-phase apply"): first the cluster + Karpenter
# controller, then the NodePool. Written here as one file for completeness.

# IAM + IRSA + the node IAM role/instance profile Karpenter needs. The community
# submodule wires the controller's pod-identity/IRSA role and the karpenter node
# role in one place.
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 20.24"

  cluster_name = module.eks.cluster_name

  # Use EKS Pod Identity (no OIDC IRSA annotation dance) for the controller.
  enable_pod_identity             = true
  create_pod_identity_association = true

  # Let Karpenter-provisioned nodes pull the worker image and register.
  create_node_iam_role          = true
  node_iam_role_name            = "${var.cluster_name}-karpenter-node"
  node_iam_role_use_name_prefix = false
  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }

  tags = local.tags
}

# Karpenter controller (Helm). Pinned chart; runs on the system node group.
resource "helm_release" "karpenter" {
  namespace        = "kube-system"
  create_namespace = false

  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = var.karpenter_version
  wait       = true

  values = [yamlencode({
    settings = {
      clusterName       = module.eks.cluster_name
      interruptionQueue = module.karpenter.queue_name
    }
    serviceAccount = {
      name = "karpenter"
    }
    controller = {
      resources = {
        requests = { cpu = "500m", memory = "512Mi" }
        limits   = { cpu = "1", memory = "1Gi" }
      }
    }
    # Keep the controller on the on-demand system group, never on spot workers.
    nodeSelector = {
      "stoker.io/role" = "system"
    }
  })]

  depends_on = [module.eks]
}

# EC2NodeClass: how Karpenter builds worker nodes — public subnets, the
# egress-only SG, the karpenter node role, and (crucially) a public IP so the
# node reaches the internet WITHOUT a NAT gateway in the data path.
resource "kubernetes_manifest" "worker_nodeclass" {
  manifest = {
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata   = { name = "stoker-worker" }
    spec = {
      # The AL2023 alias resolves to the correct per-architecture AMI on its own;
      # the arch (arm64 Graviton by default) is selected by the NodePool's
      # kubernetes.io/arch requirement (below), so one alias covers both.
      amiFamily = "AL2023"
      amiSelectorTerms = [
        { alias = "al2023@latest" }
      ]
      role = module.karpenter.node_iam_role_name

      # Public subnets (discovery tag set on the VPC module).
      subnetSelectorTerms = [
        { tags = { "karpenter.sh/discovery" = var.cluster_name } }
      ]
      # The egress-only worker SG (blocks all ingress).
      securityGroupSelectorTerms = [
        { tags = { "karpenter.sh/discovery" = var.cluster_name } }
      ]

      # Public IP so the node egresses directly (no NAT) — the whole point of
      # putting workers in public subnets.
      associatePublicIPAddress = true

      metadataOptions = {
        # Hardened IMDS: hop limit 1, tokens required.
        httpEndpoint            = "enabled"
        httpProtocolIPv6        = "disabled"
        httpPutResponseHopLimit = 1
        httpTokens              = "required"
      }

      tags = merge(local.tags, {
        "stoker.io/role"         = "worker"
        "karpenter.sh/discovery" = var.cluster_name
      })
    }
  }

  depends_on = [helm_release.karpenter]
}

# NodePool: the Graviton spot pool workers land on. Nodes are cheap cattle —
# consolidated aggressively, empty-expired quickly, and hard-capped on total CPU
# so a runaway campaign cannot scale without bound.
resource "kubernetes_manifest" "worker_nodepool" {
  manifest = {
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "stoker-workers" }
    spec = {
      template = {
        metadata = {
          labels = {
            "stoker.io/role" = "worker"
          }
        }
        spec = {
          nodeClassRef = {
            group = "karpenter.k8s.aws"
            kind  = "EC2NodeClass"
            name  = "stoker-worker"
          }
          # Only the Stoker namespace's worker pods tolerate this taint, so no
          # stray workload lands on the spot pool.
          taints = [
            {
              key    = "stoker.io/worker"
              value  = "true"
              effect = "NoSchedule"
            }
          ]
          requirements = [
            {
              key      = "kubernetes.io/arch"
              operator = "In"
              values   = [local.worker_arch_k8s]
            },
            {
              key      = "karpenter.sh/capacity-type"
              operator = "In"
              values   = var.worker_capacity_type # ["spot"]
            },
            {
              key      = "karpenter.k8s.aws/instance-family"
              operator = "In"
              values   = var.worker_instance_families
            },
            {
              key      = "kubernetes.io/os"
              operator = "In"
              values   = ["linux"]
            }
          ]
          # Give a reclaimed spot node time to run the worker drain path
          # (WORKER-CONTRACT: SIGTERM budget); spot's 2-minute notice is ample.
          terminationGracePeriod = "2m"
          expireAfter            = "168h" # recycle nodes weekly (patching)
        }
      }
      # Hard vCPU ceiling: the spend backstop alongside the Budgets alarm.
      limits = {
        cpu = tostring(var.worker_nodepool_cpu_limit)
      }
      disruption = {
        # Reclaim empty/underused nodes fast — cattle, not pets.
        consolidationPolicy = "WhenEmptyOrUnderutilized"
        consolidateAfter    = "30s"
      }
    }
  }

  depends_on = [kubernetes_manifest.worker_nodeclass]
}
