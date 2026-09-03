###############################################################################
# Stoker bootstrap: turn a fresh control plane into a configured, RUNNING one
# with no manual UI step. Drop this into the same layer as stoker.tf (it reuses
# local.namespace / local.stoker_labels / var.sok_stoker_image from there).
#
# WHAT IT DOES (all in-cluster, so it works with a private EKS API and needs no
# kubeconfig on the Terraform runner):
#   1. Ships bootstrap.py + a declarative desired-state doc as a ConfigMap.
#   2. Puts the admin credential + the target HEC token in a Secret (never in the
#      ConfigMap, never in Terraform output).
#   3. Runs a Job on the control-plane image that waits for /healthz, claims the
#      first-run admin via POST /api/auth/setup (so it wins the first-visit race
#      and the existing Deployment needs NO STOKER_ADMIN_* env), then reconciles
#      targets, pack repos, specs and launches the runs flagged run:true.
#
# The Job is idempotent (create-if-absent by name/url; a run starts only when the
# spec has none active) and re-runs whenever the script or desired state changes
# (its name carries a content hash). wait_for_completion makes `terraform apply`
# block until Stoker is bootstrapped and generating — or fail loudly if it isn't.
#
# ⚠ Ordering: on a fresh build the Job must reach setup BEFORE a human opens the
# UI, or first-run setup is already spent and the Job's login fails. As part of
# `terraform apply` right after the Deployment rolls out, it wins comfortably.
###############################################################################

variable "sok_stoker_bootstrap" {
  type        = bool
  default     = false
  description = "Run the declarative bootstrap Job (targets/packs/specs/runs) after Stoker is up."
}

variable "sok_stoker_bootstrap_hec_url" {
  type        = string
  default     = ""
  description = "HEC endpoint the bootstrap target points at (in-cluster service DNS, e.g. https://splunk-hec.splunk.svc.cluster.local:8088)."
}

variable "sok_stoker_bootstrap_hec_token" {
  type        = string
  default     = ""
  sensitive   = true
  description = "HEC token for the bootstrap target. Source it from the account layer's hec-token output / Secrets Manager, not a literal."
}

variable "sok_stoker_bootstrap_index" {
  type        = string
  default     = "loadtest"
  description = "Default index the bootstrap specs write to."
}

locals {
  stoker_bootstrap_enabled = local.stoker_enabled && var.sok_stoker_bootstrap

  # Admin credential for the control plane. Derived from pass4SymmKey exactly like
  # stoker_master_key, so it is stable across applies, needs no extra Secrets
  # Manager entry, and is not the Splunk admin password. Rotate by changing the
  # domain-separation prefix.
  stoker_admin_user = "stoker-admin"
  stoker_admin_password = substr(replace(replace(base64sha256(
    "stoker-admin-password:${data.aws_secretsmanager_secret_version.pass4symmkey.secret_string}"
  ), "+", ""), "/", ""), 0, 32)

  # The declarative desired state. Edit freely: targets, pack repos, specs, and
  # which specs run. The HEC token is injected by env (token_env), never inlined.
  stoker_bootstrap_desired = {
    targets = [{
      name          = "sok-hec"
      hec_url       = var.sok_stoker_bootstrap_hec_url
      token_env     = "STOKER_TARGET_HEC_TOKEN"
      default_index = var.sok_stoker_bootstrap_index
      env_tag       = "sok"
      verify_tls    = false
    }]
    repos = [{
      url         = "https://github.com/livehybrid/stoker-sample-packs"
      default_ref = "master"
      sync        = true
    }]
    specs = [{
      name       = "flatline-baseline"
      pack       = "flatline"
      target     = "sok-hec"
      engine     = "eventgen"
      rate_mode  = "eps"
      rate_value = 1000
      workers    = 3
      fleet      = "k8s-local"
      overrides  = { index = var.sok_stoker_bootstrap_index }
      run        = true
    }]
  }

  stoker_bootstrap_script = file("${path.module}/bootstrap.py")
  stoker_bootstrap_state  = jsonencode(local.stoker_bootstrap_desired)

  # Re-run the Job whenever the script or the desired state changes. Jobs are
  # immutable, so a new name means the old Job is replaced with a fresh run.
  stoker_bootstrap_hash = substr(sha256(
    "${local.stoker_bootstrap_script}${local.stoker_bootstrap_state}"
  ), 0, 10)
}

resource "kubernetes_secret_v1" "stoker_bootstrap" {
  count = local.stoker_bootstrap_enabled ? 1 : 0

  metadata {
    name      = "stoker-bootstrap"
    namespace = local.namespace
    labels    = local.stoker_labels
  }

  data = {
    STOKER_ADMIN_USER        = local.stoker_admin_user
    STOKER_ADMIN_PASSWORD    = local.stoker_admin_password
    STOKER_TARGET_HEC_TOKEN  = var.sok_stoker_bootstrap_hec_token
  }
}

resource "kubernetes_config_map_v1" "stoker_bootstrap" {
  count = local.stoker_bootstrap_enabled ? 1 : 0

  metadata {
    name      = "stoker-bootstrap"
    namespace = local.namespace
    labels    = local.stoker_labels
  }

  data = {
    "bootstrap.py"       = local.stoker_bootstrap_script
    "desired-state.json" = local.stoker_bootstrap_state
  }
}

resource "kubernetes_job_v1" "stoker_bootstrap" {
  count = local.stoker_bootstrap_enabled ? 1 : 0

  metadata {
    # content hash -> a changed script/state creates a fresh Job (Jobs are immutable)
    name      = "stoker-bootstrap-${local.stoker_bootstrap_hash}"
    namespace = local.namespace
    labels    = local.stoker_labels
  }

  spec {
    backoff_limit             = 3
    active_deadline_seconds   = 600
    ttl_seconds_after_finished = 3600

    template {
      metadata {
        labels = local.stoker_labels
      }

      spec {
        restart_policy = "Never"

        container {
          name  = "bootstrap"
          image = var.sok_stoker_image
          # The image's CMD is uvicorn; override it to run the script instead.
          command     = ["python3", "/bootstrap/bootstrap.py"]
          working_dir = "/bootstrap"

          env {
            name  = "STOKER_BASE_URL"
            value = "http://${local.stoker_name}:8080"
          }
          env {
            name  = "STOKER_DESIRED_STATE"
            value = "/bootstrap/desired-state.json"
          }
          # admin creds + HEC token from the Secret
          env_from {
            secret_ref {
              name = one(kubernetes_secret_v1.stoker_bootstrap[*].metadata[0].name)
            }
          }

          volume_mount {
            name       = "bootstrap"
            mount_path = "/bootstrap"
          }
        }

        volume {
          name = "bootstrap"
          config_map {
            name = one(kubernetes_config_map_v1.stoker_bootstrap[*].metadata[0].name)
          }
        }
      }
    }
  }

  # Block `terraform apply` until the bootstrap succeeds (or fails).
  wait_for_completion = true
  timeouts {
    create = "12m"
    update = "12m"
  }

  depends_on = [
    kubernetes_deployment_v1.stoker,
    kubernetes_service_v1.stoker,
    kubernetes_config_map_v1.stoker_bootstrap,
    kubernetes_secret_v1.stoker_bootstrap,
  ]
}

output "stoker_admin_user" {
  value       = local.stoker_bootstrap_enabled ? local.stoker_admin_user : null
  description = "Admin username the bootstrap created (password is derived; retrieve from the stoker-bootstrap Secret)."
}
