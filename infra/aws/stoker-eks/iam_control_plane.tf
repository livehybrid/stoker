# Dedicated IAM principal for the on-prem Stoker control plane.
#
# Design section 11: "a dedicated IAM user ... whose only permission is
# eks:DescribeCluster, mapped via an EKS access entry to the namespaced stoker
# Role. The kubeconfig uses exec auth (aws eks get-token); the access keys live
# Fernet-encrypted in fleets.config_json ... with a documented rotation step."
#
# The IAM permission (eks:DescribeCluster) is deliberately tiny: it lets the
# control plane discover the endpoint + CA and mint a token. ALL cluster power
# comes from the EKS access entry below, which maps this principal to the RBAC
# group "stoker:control-plane" — bound (rbac.tf) to the least-privilege Role.
# So even a leaked access key can only create/list/watch/delete Jobs/Pods/
# Secrets in one namespace, never touch other AWS resources, never cluster-admin.

resource "aws_iam_user" "control_plane" {
  name = var.control_plane_principal_name
  tags = local.tags
}

# The ONLY IAM permission: describe THIS cluster (needed for `aws eks get-token`
# / endpoint discovery). Scoped to the single cluster ARN, not eks:* on "*".
data "aws_iam_policy_document" "control_plane_describe" {
  statement {
    sid       = "DescribeThisClusterOnly"
    effect    = "Allow"
    actions   = ["eks:DescribeCluster"]
    resources = [module.eks.cluster_arn]
  }
}

resource "aws_iam_user_policy" "control_plane_describe" {
  name   = "${var.cluster_name}-describe-cluster-only"
  user   = aws_iam_user.control_plane.name
  policy = data.aws_iam_policy_document.control_plane_describe.json
}

# Access key for the control plane. Sensitive: the secret is a marked output;
# feed it (via SOPS) into fleets.config_json for fleet "eks", then rotate per
# the README. Toggle off (create_control_plane_access_key=false) to attach a key
# out-of-band instead.
resource "aws_iam_access_key" "control_plane" {
  count = var.create_control_plane_access_key ? 1 : 0
  user  = aws_iam_user.control_plane.name
}

# EKS access entry: bind the IAM user to the in-cluster RBAC group the namespaced
# Role is bound to. This is the whole of the control plane's cluster authority.
resource "aws_eks_access_entry" "control_plane" {
  cluster_name      = module.eks.cluster_name
  principal_arn     = aws_iam_user.control_plane.arn
  kubernetes_groups = ["stoker:control-plane"]
  type              = "STANDARD"

  tags = local.tags
}

# NOTE: no aws_eks_access_policy_association here on purpose. AWS-managed access
# policies (e.g. AmazonEKSClusterAdminPolicy) would grant broad cluster power;
# instead the principal's rights come ENTIRELY from the namespaced Role via the
# "stoker:control-plane" group binding in rbac.tf. Least privilege by
# construction.
