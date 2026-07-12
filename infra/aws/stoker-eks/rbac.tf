# In-cluster RBAC for Stoker: the "stoker" namespace, a least-privilege
# namespaced Role, a ServiceAccount and the binding.
#
# The control-plane IAM principal is mapped (via an EKS access entry, see
# iam_control_plane.tf) to the RBAC group "stoker:control-plane", which this
# Role is bound to. Result: the control plane can create/list/watch/delete only
# Jobs, Pods and Secrets, only in this namespace — NOT cluster-admin (design
# section 11 + 14 least-privilege).

resource "kubernetes_namespace" "stoker" {
  metadata {
    name = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"       = "terraform"
      "app.kubernetes.io/part-of"          = "stoker"
      "pod-security.kubernetes.io/enforce" = "baseline"
    }
  }

  depends_on = [module.eks]
}

# A ServiceAccount for the namespace. Note: worker pods set
# automountServiceAccountToken=false and never use this — it exists as the
# named subject the control-plane access entry can also target if a token-based
# (rather than access-entry-RBAC-group) path is ever wanted. Named to match the
# k3s reference manifest (infra/k8s/rbac.yaml): "stoker-driver".
resource "kubernetes_service_account" "stoker_driver" {
  metadata {
    name      = "stoker-driver"
    namespace = kubernetes_namespace.stoker.metadata[0].name
  }
  automount_service_account_token = false
}

# Least-privilege Role: exactly the verbs the K8sDriver needs, on exactly the
# resource kinds it touches. No get on secrets' values beyond create/delete
# lifecycle; no cluster-scoped rights; no wildcards.
resource "kubernetes_role" "stoker_driver" {
  metadata {
    name      = "stoker-driver"
    namespace = kubernetes_namespace.stoker.metadata[0].name
  }

  # Jobs (batch/v1): create + read + scale (patch/update) + delete + watch.
  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["create", "get", "list", "watch", "patch", "update", "delete"]
  }

  # Pods: list/watch/get for status + logs; delete for reap. (Reads only; the
  # driver never execs into pods.)
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list", "watch", "delete"]
  }

  # Pod logs: the driver's logs() reads them for the live-tail tab.
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get", "list"]
  }

  # Secrets: create the per-run ephemeral HEC-token Secret + patch its
  # ownerReference + delete. No standing read of secret values is required
  # (the Job's secretKeyRef resolves server-side).
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create", "get", "list", "watch", "patch", "update", "delete"]
  }
}

# Bind the Role to BOTH the ServiceAccount and the RBAC group the control-plane
# access entry maps to. The group binding is the live path; the SA binding keeps
# the in-namespace SA usable if ever needed.
resource "kubernetes_role_binding" "stoker_driver" {
  metadata {
    name      = "stoker-driver"
    namespace = kubernetes_namespace.stoker.metadata[0].name
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.stoker_driver.metadata[0].name
  }

  # The RBAC group the EKS access entry assigns to the control-plane principal.
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = "stoker:control-plane"
  }

  # The in-namespace ServiceAccount (belt-and-braces).
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.stoker_driver.metadata[0].name
    namespace = kubernetes_namespace.stoker.metadata[0].name
  }
}

# --------------------------------------------------------------------------- #
# NetworkPolicy: workers are egress-only (design section 5 + 14 hardening,
# "ships with the driver, not later"). Belongs with the cluster IaC so a fresh
# cluster is safe before the first run. Requires a CNI that enforces
# NetworkPolicy (EKS: the VPC CNI network-policy add-on, or Cilium).
# --------------------------------------------------------------------------- #

resource "kubernetes_manifest" "worker_egress_only_netpol" {
  manifest = {
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata = {
      name      = "stoker-workers-egress-only"
      namespace = local.namespace
    }
    spec = {
      # Applies to worker pods (labelled stoker.run=<id> by the driver).
      podSelector = {
        matchExpressions = [
          { key = "stoker.run", operator = "Exists" }
        ]
      }
      policyTypes = ["Ingress", "Egress"]
      # No ingress rules => deny all inbound to worker pods.
      ingress = []
      # Egress: DNS (in-cluster) + everything else the worker needs (HEC target
      # + control plane) over the internet. The design's blast-radius statement:
      # egress-only + a single-target token bounds a compromised trusted-code
      # pack; tighten the CIDR here to the known HEC/control-plane egress if the
      # target set is fixed.
      egress = [
        {
          # In-cluster DNS.
          to = [
            { namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } } }
          ]
          ports = [
            { protocol = "UDP", port = 53 },
            { protocol = "TCP", port = 53 }
          ]
        },
        {
          # HEC (HTTPS/HEC) + control-plane heartbeats/bundle GETs over the
          # internet. Left broad (0.0.0.0/0 on 443/8088) because targets vary;
          # narrow to specific CIDRs for a locked-down campaign.
          to = [{ ipBlock = { cidr = "0.0.0.0/0" } }]
          ports = [
            { protocol = "TCP", port = 443 },
            { protocol = "TCP", port = 8088 }
          ]
        }
      ]
    }
  }

  depends_on = [kubernetes_namespace.stoker]
}
