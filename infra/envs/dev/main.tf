variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "loka-test"
}

variable "environment" {
  type = string
}

data "aws_caller_identity" "current" {}

locals {
  prefix = "${var.project}-${var.environment}"
  suffix = "${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

module "source_bucket" {
  source = "../../modules/s3_bucket"
  name   = "${local.prefix}-source-${local.suffix}"
}

module "target_bucket" {
  source = "../../modules/s3_bucket"
  name   = "${local.prefix}-target-${local.suffix}"
}

output "source_bucket" {
  value = module.source_bucket.name
}

output "target_bucket" {
  value = module.target_bucket.name
}