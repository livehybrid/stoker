# Outputs — what the on-prem control plane needs to register fleet "eks", plus
# the sensitive control-plane credential. Secrets are marked sensitive; consume
# them through SOPS into fleets.config_json, never echo them into logs.

output "cluster_name" {
  description = "EKS cluster name (for `aws eks get-token --cluster-name`)."
  value       = module.eks.cluster_name
}

output "cluster_arn" {
  description = "EKS cluster ARN."
  value       = module.eks.cluster_arn
}

output "cluster_endpoint" {
  description = "EKS API server endpoint (control plane discovers it via eks:DescribeCluster)."
  value       = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  description = "Base64 cluster CA bundle (for the exec-auth kubeconfig)."
  value       = module.eks.cluster_certificate_authority_data
}

output "region" {
  description = "AWS region (the kubeconfig exec args need it)."
  value       = var.region
}

output "namespace" {
  description = "The namespace the K8sDriver runs Jobs in (fleets.config_json.namespace)."
  value       = local.namespace
}

output "kms_key_arn" {
  description = "KMS CMK ARN used for EKS secrets envelope encryption."
  value       = aws_kms_key.eks.arn
}

output "control_plane_principal_arn" {
  description = "IAM user ARN mapped (eks:DescribeCluster only) to the namespaced Role via an access entry."
  value       = aws_iam_user.control_plane.arn
}

output "budget_sns_topic_arn" {
  description = "SNS topic the (non-optional) Budgets alarm publishes to."
  value       = aws_sns_topic.budget_alerts.arn
}

# --- Sensitive: the control-plane access key -------------------------------- #
# Retrieve with `terraform output -raw control_plane_access_key_id` /
# `... control_plane_secret_access_key`, encrypt via SOPS into
# fleets.config_json for fleet "eks", then ROTATE per the README.

output "control_plane_access_key_id" {
  description = "Access key id for the control-plane IAM user (null if not created here)."
  value       = try(aws_iam_access_key.control_plane[0].id, null)
}

output "control_plane_secret_access_key" {
  description = "Secret access key for the control-plane IAM user (SENSITIVE; null if not created here)."
  value       = try(aws_iam_access_key.control_plane[0].secret, null)
  sensitive   = true
}

# A ready-to-encrypt kubeconfig using exec auth (no long-lived token embedded).
# The control plane can also synthesise this itself from the outputs above; this
# output is a convenience for the initial fleet registration.
output "exec_kubeconfig" {
  description = "kubeconfig (exec-auth via `aws eks get-token`) for fleet registration. SENSITIVE only in that it names the cluster; contains no token."
  value = yamlencode({
    apiVersion      = "v1"
    kind            = "Config"
    current-context = module.eks.cluster_name
    clusters = [{
      name = module.eks.cluster_name
      cluster = {
        server                     = module.eks.cluster_endpoint
        certificate-authority-data = module.eks.cluster_certificate_authority_data
      }
    }]
    contexts = [{
      name = module.eks.cluster_name
      context = {
        cluster   = module.eks.cluster_name
        namespace = local.namespace
        user      = "stoker-control-plane"
      }
    }]
    users = [{
      name = "stoker-control-plane"
      user = {
        exec = {
          apiVersion = "client.authentication.k8s.io/v1beta1"
          command    = "aws"
          args = [
            "eks", "get-token",
            "--cluster-name", module.eks.cluster_name,
            "--region", var.region,
          ]
          # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY for the control-plane user
          # are injected by the control plane at exec time from the Fernet-
          # decrypted fleets.config_json (never written here).
        }
      }
    }]
  })
}
