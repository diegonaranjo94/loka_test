# ---------- who may assume each role ----------
data "aws_iam_policy_document" "trust_plan" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["repo:${var.github_repo}:pull_request"]
    }
  }
}

data "aws_iam_policy_document" "trust_apply" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["repo:${var.github_repo}:environment:${var.github_environment}"]
    }
  }
}

# ---------- plan role: read-only + state lock (used by PRs) ----------
resource "aws_iam_role" "gh_plan" {
  name               = "${var.project}-gh-plan"
  assume_role_policy = data.aws_iam_policy_document.trust_plan.json
}

resource "aws_iam_role_policy_attachment" "gh_plan_readonly" {
  role       = aws_iam_role.gh_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "plan_state" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.tfstate.arn]
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.tfstate.arn}/*"]
  }
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.tfstate.arn}/*.tflock"]
  }
}

resource "aws_iam_role_policy" "gh_plan_state" {
  name   = "terraform-state"
  role   = aws_iam_role.gh_plan.id
  policy = data.aws_iam_policy_document.plan_state.json
}

# ---------- boundary: the ceiling for every role the platform creates later ----------
data "aws_iam_policy_document" "workload_boundary" {
  statement {
    sid = "PlatformRuntime"
    actions = [
      "s3:*", "glue:*", "athena:*", "logs:*", "states:StartExecution",
      "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey",
      "ecr:GetAuthorizationToken", "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability",
      "ecs:RunTask", "ecs:StopTask", "ecs:DescribeTasks",
      "events:PutRule", "events:PutTargets", "events:DescribeRule",
      "sns:Publish", "cloudwatch:PutMetricData", "iam:PassRole",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "workload_boundary" {
  name   = "${var.project}-workload-boundary"
  policy = data.aws_iam_policy_document.workload_boundary.json
}

# ---------- apply role: deploys the platform (used after your approval) ----------
locals {
  project_roles    = "arn:aws:iam::${local.account_id}:role/${var.project}-*"
  project_policies = "arn:aws:iam::${local.account_id}:policy/${var.project}-*"
  gh_roles         = "arn:aws:iam::${local.account_id}:role/${var.project}-gh-*"
}

data "aws_iam_policy_document" "apply" {
  statement {
    sid = "PlatformServices"
    actions = [
      "s3:*", "kms:*", "ecr:*", "ecs:*", "ec2:*", "states:*", "scheduler:*",
      "events:*", "sns:*", "glue:*", "athena:*", "logs:*", "cloudwatch:*",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "IamRead"
    actions   = ["iam:Get*", "iam:List*"]
    resources = ["*"]
  }

  statement {
    sid = "IamRolesOnlyWithBoundary"
    actions = [
      "iam:CreateRole", "iam:PutRolePermissionsBoundary",
      "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy",
    ]
    resources = [local.project_roles]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.workload_boundary.arn]
    }
  }

  statement {
    sid = "IamRoleLifecycle"
    actions = [
      "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
      "iam:TagRole", "iam:UntagRole", "iam:PassRole",
    ]
    resources = [local.project_roles]
  }

  statement {
    sid = "IamProjectPolicies"
    actions = [
      "iam:CreatePolicy", "iam:DeletePolicy", "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion", "iam:TagPolicy", "iam:UntagPolicy",
    ]
    resources = [local.project_policies]
  }

  statement {
    sid       = "EcsServiceLinkedRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["ecs.amazonaws.com"]
    }
  }

  statement {
    sid       = "ProtectDeployRolesAndBoundary"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = [local.gh_roles, aws_iam_policy.workload_boundary.arn]
  }

  statement {
    sid       = "NoBoundaryRemoval"
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "gh_apply" {
  name                 = "${var.project}-gh-apply"
  assume_role_policy   = data.aws_iam_policy_document.trust_apply.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "gh_apply" {
  name   = "deploy-platform"
  role   = aws_iam_role.gh_apply.id
  policy = data.aws_iam_policy_document.apply.json
}