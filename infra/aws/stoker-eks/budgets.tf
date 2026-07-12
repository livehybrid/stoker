# AWS Budgets alarm — NON-OPTIONAL (design section 11: "An AWS Budgets alarm
# (SNS to email) is a non-optional module resource").
#
# Backstops the cluster-as-cattle convention: every auto-abort policy checks
# health, none checks spend, so a forgotten cluster / soak must trip a hard
# spend edge. Fires at each threshold on BOTH actual and forecast spend, to the
# operator's email via SNS.

resource "aws_sns_topic" "budget_alerts" {
  name = "${var.cluster_name}-budget-alerts"
  tags = local.tags
}

resource "aws_sns_topic_subscription" "budget_email" {
  topic_arn = aws_sns_topic.budget_alerts.arn
  protocol  = "email"
  endpoint  = var.budget_alert_email
  # NOTE: an email subscription must be confirmed once (AWS sends a
  # confirmation link on first apply). Documented in the README.
}

# Allow AWS Budgets to publish to the topic.
data "aws_iam_policy_document" "budget_sns" {
  statement {
    sid       = "AllowBudgetsPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.budget_alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "budget_alerts" {
  arn    = aws_sns_topic.budget_alerts.arn
  policy = data.aws_iam_policy_document.budget_sns.json
}

resource "aws_budgets_budget" "monthly_cost" {
  name         = "${var.cluster_name}-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # One notification per threshold, on actual spend...
  dynamic "notification" {
    for_each = var.budget_alert_thresholds_pct
    content {
      comparison_operator       = "GREATER_THAN"
      threshold                 = notification.value
      threshold_type            = "PERCENTAGE"
      notification_type         = "ACTUAL"
      subscriber_sns_topic_arns = [aws_sns_topic.budget_alerts.arn]
    }
  }

  # ...and on forecast spend, so an about-to-overrun month warns early.
  dynamic "notification" {
    for_each = var.budget_alert_thresholds_pct
    content {
      comparison_operator       = "GREATER_THAN"
      threshold                 = notification.value
      threshold_type            = "PERCENTAGE"
      notification_type         = "FORECASTED"
      subscriber_sns_topic_arns = [aws_sns_topic.budget_alerts.arn]
    }
  }

  tags = local.tags
}
