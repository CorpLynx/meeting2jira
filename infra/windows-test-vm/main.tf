# A throwaway Windows Server host for the checks that cannot run on macOS or Linux:
# real Windows PowerShell 5.1 parsing, DPAPI, the PS -> Python JSON handoff, Constrained
# Language Mode, and Task Scheduler.
#
# Access model: no inbound rules at all, no key pair, no RDP on the internet. Everything
# goes through SSM Session Manager over the instance's outbound HTTPS connection.
#
# Networking: this reuses an existing VPC/subnet (var.subnet_id) rather than creating a
# dedicated VPC, IGW, and route table for a lab that gets destroyed the same day. The chosen
# subnet must already route 0.0.0.0/0 to an internet gateway and auto-assign public IPs (or
# NAT/VPC-endpoint egress must exist some other way) -- SSM and the installers need outbound
# HTTPS. This stack only adds a security group; it does not touch the existing VPC's route
# tables or other resources.

data "aws_ssm_parameter" "windows" {
  name = var.windows_ami_parameter
}

data "aws_subnet" "chosen" {
  id = var.subnet_id
}

data "aws_caller_identity" "current" {}

resource "random_id" "suffix" {
  byte_length = 4
}

# ---------------------------------------------------------------------------------------------
# Security group in the existing VPC. Egress only. There is intentionally no ingress block:
# an empty ingress set denies all inbound traffic, which is what makes the missing RDP
# exposure safe.
# ---------------------------------------------------------------------------------------------
resource "aws_security_group" "instance" {
  name        = "${var.name}-egress-only"
  description = "Outbound only; inbound access is via SSM Session Manager, not the network"
  vpc_id      = data.aws_subnet.chosen.vpc_id

  egress {
    description = "Outbound HTTPS for SSM, and HTTP/HTTPS to fetch the Python installer"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = var.name }
}

# ---------------------------------------------------------------------------------------------
# Transport for the repo and for command output. SSM inline output is truncated at 24 KB,
# which the verbose unittest run exceeds, so results land in S3 instead.
# ---------------------------------------------------------------------------------------------
resource "aws_s3_bucket" "payload" {
  bucket        = "${var.name}-${random_id.suffix.hex}"
  force_destroy = true # a lab bucket must not block `terraform destroy`
}

resource "aws_s3_bucket_public_access_block" "payload" {
  bucket                  = aws_s3_bucket.payload.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "payload" {
  bucket = aws_s3_bucket.payload.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------------------------------------------------------------------------------------------
# Instance identity: SSM management plus read/write on this one bucket. Nothing else.
# ---------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name}-${random_id.suffix.hex}"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "payload_access" {
  statement {
    sid       = "ListTheLabBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.payload.arn]
  }

  statement {
    sid       = "ReadTheRepoAndWriteResults"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.payload.arn}/*"]
  }
}

resource "aws_iam_role_policy" "payload_access" {
  name   = "lab-bucket-access"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.payload_access.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name}-${random_id.suffix.hex}"
  role = aws_iam_role.instance.name
}

# ---------------------------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------------------------
resource "random_password" "standard_user" {
  length           = 24
  special          = true
  override_special = "!@#$%^&*()-_=+"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2
}

resource "aws_instance" "this" {
  ami                         = nonsensitive(data.aws_ssm_parameter.windows.value)
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.instance.id]
  iam_instance_profile        = aws_iam_instance_profile.instance.name
  associate_public_ip_address = true

  # Shut down (not terminate) on `shutdown /s`, so the auto-stop task saves compute cost
  # without destroying the box you are debugging on.
  instance_initiated_shutdown_behavior = "stop"

  user_data = templatefile("${path.module}/bootstrap.ps1.tftpl", {
    python_version     = var.python_version
    bucket             = aws_s3_bucket.payload.bucket
    region             = var.region
    auto_stop_hours    = var.auto_stop_hours
    standard_user_name = var.standard_user_name
    standard_user_pass = random_password.standard_user.result
  })

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  tags = { Name = var.name }
}
