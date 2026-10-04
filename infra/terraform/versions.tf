terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = "swarvaah-ai", Environment = var.environment, ManagedBy = "terraform" }
  }
}

variable "aws_region" {
  type = string
  default = "ap-south-1"
  validation {
    condition = var.aws_region == "ap-south-1"
    error_message = "Customer data infrastructure must remain in the Mumbai region."
  }
}

variable "environment" {
  type = string
  default = "pilot"
}
