output "instance_id" {
  description = "Target for `aws ssm start-session` and for the run-checks helper."
  value       = aws_instance.this.id
}

output "region" {
  description = "Region the instance lives in."
  value       = var.region
}

output "bucket" {
  description = "Lab bucket holding the synced repo and the check output."
  value       = aws_s3_bucket.payload.bucket
}

output "standard_user_name" {
  description = "The non-administrator account created at boot."
  value       = var.standard_user_name
}

output "standard_user_password" {
  description = <<-EOT
    Password for the non-admin account. Read it with:
      terraform output -raw standard_user_password
    This value is stored in terraform.tfstate in plaintext, which is acceptable only because
    the host is a throwaway lab with no inbound network access. Do not reuse it anywhere.
  EOT
  value       = random_password.standard_user.result
  sensitive   = true
}

output "session_command" {
  description = "Open an interactive PowerShell session on the host (no RDP, no inbound ports)."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.this.id}"
}

output "rdp_port_forward_command" {
  description = <<-EOT
    Only needed for the interactive Task Scheduler checks. Tunnels RDP over SSM, then connect
    a client to localhost:13389 as the standard user. Still no inbound security-group rule.
  EOT
  value = join(" ", [
    "aws ssm start-session --region ${var.region} --target ${aws_instance.this.id}",
    "--document-name AWS-StartPortForwardingSession",
    "--parameters 'portNumber=3389,localPortNumber=13389'",
  ])
}
