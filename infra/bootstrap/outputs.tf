output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

output "plan_role_arn" {
  value = aws_iam_role.gh_plan.arn
}

output "apply_role_arn" {
  value = aws_iam_role.gh_apply.arn
}

output "aws_region" {
  value = var.aws_region
}