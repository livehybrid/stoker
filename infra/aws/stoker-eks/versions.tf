# Terraform + provider version pins for the Stoker EKS module.
#
# Pinned deliberately: this module is applied by hand at campaign start
# (cluster-as-cattle, see README) and must resolve to the same provider majors
# every time so a `terraform apply` months apart does not silently pick up a
# breaking provider release. Bump these consciously.
#
# UNAPPLIED: there is no AWS backend wired here. A remote state backend (S3 +
# DynamoDB lock, or Terraform Cloud) should be added before first real apply so
# the on-prem control plane and any operator share one state. Left as a local
# backend on purpose so the skeleton stands alone; see README "State".

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40.0, < 6.0.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.27.0, < 3.0.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.13.0, < 3.0.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0.0, < 5.0.0"
    }
  }

  # backend "s3" {}  # wire remote state before first real apply (see README)
}
