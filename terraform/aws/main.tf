# Layer 1 on AWS: one small ARM server running k3s (a lightweight Kubernetes), built to stay
# inside the AWS free plan's credits. It replaces the kind cluster on your laptop; the platform
# layer (layer 2) deploys onto it unchanged.
#
#   .\scripts\cloud-up.ps1      build this, then deploy the platform onto it
#   .\scripts\cloud-down.ps1    delete everything this file created
#
# Security choices:
# - Nothing is open to the internet. The firewall has NO inbound rules: not SSH, not the
#   Kubernetes API, not Grafana. You reach the server through AWS Systems Manager (an
#   encrypted tunnel opened with your AWS login), so there is no port to scan or attack.
# - Pods cannot borrow the server's AWS permissions. The metadata service that hands them out
#   only answers the server itself (IMDSv2 with a hop limit of 1), so a compromised pod
#   gets nothing.
# - The server's own permissions are the minimum: Systems Manager, and writing backups into
#   one bucket.
# - The disk is encrypted, and k3s encrypts Kubernetes secrets at rest.
#
# Cost (us-east-1, on-demand, roughly): t4g.small ~$12/month + public IPv4 ~$3.65/month +
# 20 GB disk ~$1.60/month + backups, pennies. About $18 a month, paid from the free plan's
# credits. A budget alarm emails you if spending goes above that.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  description = "AWS region. us-east-1 is usually the cheapest."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "t4g.small: 2 ARM cores, 2 GB memory, eligible for the free plan."
  type        = string
  default     = "t4g.small"
}

variable "alert_email" {
  description = "Where the budget alarm is emailed. Set it in terraform.tfvars (not committed)."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Spending above this in a month (before credits) sends an email."
  type        = number
  default     = 20
}

variable "k3s_version" {
  description = "Pinned, so a rebuild gets the same Kubernetes."
  type        = string
  default     = "v1.35.10+k3s1"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "portfolio-agent", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "me" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# Amazon Linux 2023 for ARM: the AWS command line and the Systems Manager agent come built in
data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# ---------- Network: a small private network with a route out, and nothing in ----------
resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "portfolio-agent" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true # outbound internet without a NAT gateway (which would cost ~$33/month)
  tags                    = { Name = "portfolio-agent-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "server" {
  name        = "portfolio-agent-server"
  description = "No inbound access at all; outbound HTTPS and DNS for Alpaca, Gemini, Discord, images"
  vpc_id      = aws_vpc.main.id

  # No ingress blocks: nothing on the internet can open a connection to this server.

  egress {
    description = "HTTPS (APIs, container images, Systems Manager)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "HTTP (operating system package mirrors)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "DNS"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "Time sync"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------- Backups: off the server, so losing the server doesn't lose the data ----------
resource "aws_s3_bucket" "backups" {
  bucket_prefix = "portfolio-agent-backups-"
  force_destroy = false # cloud-down.ps1 empties it on purpose, after offering to download it
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "keep-30-days"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

# ---------- The server's permissions: the minimum it needs ----------
resource "aws_iam_role" "server" {
  name = "portfolio-agent-server"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "backups" {
  name = "write-backups"
  role = aws_iam_role.server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.backups.arn}/postgres/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.backups.arn },
    ]
  })
}

resource "aws_iam_instance_profile" "server" {
  name = "portfolio-agent-server"
  role = aws_iam_role.server.name
}

# ---------- The server ----------
resource "aws_instance" "server" {
  ami                    = data.aws_ssm_parameter.ami.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.server.id]
  iam_instance_profile   = aws_iam_instance_profile.server.name

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1          # pods are one hop further away, so they can't reach it
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  credit_specification {
    cpu_credits = "standard" # never bill for CPU bursts beyond what the instance earns
  }

  user_data = templatefile("${path.module}/server-setup.sh.tftpl", {
    k3s_version = var.k3s_version
    region      = var.region
    bucket      = aws_s3_bucket.backups.bucket
  })
  user_data_replace_on_change = true

  tags = { Name = "portfolio-agent" }

  lifecycle {
    ignore_changes = [ami] # a newer Amazon Linux release shouldn't rebuild the server by itself
  }
}

# ---------- Spending alarm ----------
resource "aws_budgets_budget" "monthly" {
  name         = "portfolio-agent-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false # show what the project really costs, not the total after credits (~$0)
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}

output "instance_id" {
  value = aws_instance.server.id
}

output "region" {
  value = var.region
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "account_id" {
  value = data.aws_caller_identity.me.account_id
}
