variable "region" {
  description = "AWS region for the throwaway test VM."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Name prefix for every resource this stack creates."
  type        = string
  default     = "m2j-testvm"
}

variable "subnet_id" {
  description = <<-EOT
    Existing subnet to launch into. Deliberately has no default: it is account-specific, so a
    hardcoded value would neither work for anyone else nor belong in a shared repository.

    The subnet must reach the internet outbound, because SSM and the Python/AWS CLI installers
    need it. A public subnet that auto-assigns public IPs and routes 0.0.0.0/0 to an internet
    gateway is the cheap option. Find a candidate with:

      aws ec2 describe-subnets \
        --query 'Subnets[?MapPublicIpOnLaunch].{id:SubnetId,az:AvailabilityZone,vpc:VpcId}' \
        --output table

    Then confirm its route table has a 0.0.0.0/0 route to an igw-*, and pass it as
    -var subnet_id=subnet-xxxxxxxx (or put it in a gitignored *.tfvars file).
  EOT
  type        = string

  validation {
    condition     = can(regex("^subnet-[0-9a-f]+$", var.subnet_id))
    error_message = "subnet_id must look like subnet-0123456789abcdef0."
  }
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. Windows Server needs headroom; t3.large (2 vCPU / 8 GiB) is the
    smallest size that installs Python and runs the suite without swapping.
  EOT
  type        = string
  default     = "t3.large"
}

variable "windows_ami_parameter" {
  description = <<-EOT
    SSM public parameter naming the AMI. Windows Server 2022 ships Windows PowerShell 5.1,
    which is the runtime this repo targets. Do not switch to a Core image: Core omits
    parts of the shell and Task Scheduler UI surface we want to exercise.
  EOT
  type        = string
  default     = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
}

variable "python_version" {
  description = <<-EOT
    python.org version installed at boot. The repo promises 3.8+, so set this to "3.8.10"
    when you want to prove the floor, and leave it current for realistic runtime behavior.
  EOT
  type        = string
  default     = "3.12.10"
}

variable "root_volume_gb" {
  description = "Root EBS volume size in GiB. The Windows AMI needs ~30."
  type        = number
  default     = 30

  validation {
    condition     = var.root_volume_gb >= 30
    error_message = "The Windows Server AMI does not fit in less than 30 GiB."
  }
}

variable "auto_stop_hours" {
  description = <<-EOT
    Hours after each boot before the instance shuts itself down, so a forgotten lab stops
    billing compute. Set to 0 to disable. Shutdown stops the instance; it does not delete
    it, so `terraform destroy` is still how you clean up.
  EOT
  type        = number
  default     = 4

  validation {
    condition     = var.auto_stop_hours >= 0 && var.auto_stop_hours <= 24
    error_message = "auto_stop_hours must be between 0 and 24."
  }
}

variable "standard_user_name" {
  description = <<-EOT
    A deliberately non-administrator local account. SSM runs commands as SYSTEM, which is
    an administrator, so anything claiming to verify the "no admin rights" requirement has
    to run as this account instead.
  EOT
  type        = string
  default     = "m2juser"
}
