variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "loka-test"
}

variable "github_repo" {
  description = "owner/repo, exactly as on GitHub"
  type        = string
}

variable "github_environment" {
  type    = string
  default = "dev"
}