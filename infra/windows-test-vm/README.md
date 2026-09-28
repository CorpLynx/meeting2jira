# Windows verification lab

A throwaway Windows Server 2022 EC2 instance for the checks that cannot run on macOS or Linux.
Create it, run the checks, destroy it.

## What it proves, and what it does not

Verified here:

- **Real Windows PowerShell 5.1 parsing.** The 5.1 parser rejects PS7-only syntax outright, which
  an AST walk under pwsh 7 only approximates.
- **DPAPI credential storage.** `credstore.py`'s ctypes calls into `CryptProtectData` actually
  execute, and the ciphertext is checked for plaintext leakage.
- **The PowerShell to Python export handoff**, for 0, 1, and 2 meetings, including whether this
  build wraps arrays as `{"value":[...],"Count":n}`.
- **The CSV push path end to end**, and the `meeting2jira.cmd` entry point (batch has no
  parse-only mode, so it has to be executed).
- **`Test-Environment.ps1` at runtime**, including the `last_run.json` health block, checked
  against planted recent, failed, and stale files. This is where a local-vs-UTC mistake in the
  staleness age showed up.
- **The scheduled task's start time** derived from `tour_of_duty.end`.

**Not** verified here, and still needing a real workstation:

- **The Outlook COM export.** The base AMI has no Office, and installing M365 Apps on Windows
  Server needs Shared Computer Activation plus a licensed mailbox and a mail profile. That leaves
  the `Restrict` locale filter, recurrence expansion, and the object-model guard unproven — which
  is HANDOFF risk #1, #2, and #5.
- **The `Register-ScheduledTask` call itself.** The task uses `LogonType Interactive`, and under
  SSM the caller is SYSTEM, whose `USERDOMAIN\USERNAME` is the machine account and has no
  interactive SID — it fails with "No mapping between account names and security IDs". Check 8
  reports this as a known limitation rather than a failure. Use the RDP port-forward below, or
  your own machine.
- Anything agency-specific: AppLocker, TLS inspection, PAC proxies, GPO-enforced policy.

## Security model

- **No inbound rules.** The security group has an empty ingress set. There is no RDP on the
  internet, no key pair, and no SSH.
- Access is via **SSM Session Manager** over the instance's outbound HTTPS connection.
- IMDSv2 required, encrypted root volume, and an instance role limited to
  `AmazonSSMManagedInstanceCore` plus read/write on this lab's own S3 bucket.
- It **reuses an existing VPC and subnet** rather than creating its own, so it adds one security
  group and touches nothing else in the account. The subnet needs outbound HTTPS (a public subnet
  with an internet gateway, which is what `var.subnet_id` defaults to).

`terraform.tfstate` holds the lab's local-account password in plaintext. It is gitignored. Treat
the whole host as disposable and never put real credentials on it.

## Use

```bash
terraform init
terraform apply                 # ~11 resources
./run-checks.sh                 # sync this checkout up, run the checks, print results
terraform destroy               # when finished
```

`run-checks.sh` waits for the SSM agent, uploads **`app/`** to S3 (excluding secrets and local
state), runs `tools/Invoke-WindowsChecks.ps1` under real `powershell.exe`, and prints the output with
the exit code. It ships only the program folder, and the *working tree* rather than a commit, so the
loop stays edit → run.

The checks are invoked without `-ExecutionPolicy Bypass`. Files arriving via `aws s3 sync` carry no
zone marking, so the default `RemoteSigned` policy runs them; a failure there would be a real
finding rather than something worked around.

## Cost

A `t3.large` Windows instance is roughly $0.10–0.12/hour on demand in `us-east-1`, plus a little
for the 30 GB gp3 volume. The bootstrap arms a scheduled task that **stops** the instance
`var.auto_stop_hours` (default 4) after each boot so a forgotten lab stops billing compute.
Stopping is not deleting: run `terraform destroy` to remove everything.

## Interactive access

```bash
# a shell on the host
aws ssm start-session --region us-east-1 --target "$(terraform output -raw instance_id)"

# RDP through the tunnel, for the Task Scheduler checks, then connect to localhost:13389
terraform output -raw rdp_port_forward_command
terraform output -raw standard_user_password
```

Log in as the non-administrator account (`terraform output standard_user_name`) when you need to
confirm something works **without** admin rights. SSM itself runs as SYSTEM, so it cannot tell you
anything about the no-admin requirement.

## Variables worth knowing

| Variable | Default | Why change it |
|---|---|---|
| `subnet_id` | the account's existing public subnet | A different VPC, or if that subnet is gone. |
| `python_version` | `3.12.10` | Set `3.8.10` to test the version floor the project promises. |
| `auto_stop_hours` | `4` | `0` disables the auto-stop. |
| `instance_type` | `t3.large` | Smaller struggles with Windows plus the installers. |
