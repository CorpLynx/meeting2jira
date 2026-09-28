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
    Existing subnet to launch into. Must auto-assign public IPs and route 0.0.0.0/0 to an
    internet gateway (or otherwise have outbound HTTPS), since SSM and the installers need
    it. Defaults to the public subnet already in this account's vpc-09d04c6eb9b424f9e
    (10.20.1.0/24, us-east-1a). Override if that subnet ever changes or you'd rather use a
    different VPC.
  EOT
  type        = string
  default     = "subnet-0ac0b218bf9d0e695"
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
