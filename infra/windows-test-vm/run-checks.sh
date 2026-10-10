#!/usr/bin/env bash
#
# Sync this checkout to the lab VM and run tools/Invoke-WindowsChecks.ps1 on it under real
# Windows PowerShell 5.1. Prints the output back here.
#
# Usage:  ./run-checks.sh [-- extra args for Invoke-WindowsChecks.ps1]
#
# Requires: terraform apply already done in this directory, and the AWS CLI on PATH.
# Everything goes over SSM. No inbound ports, no RDP, no SSH key.

set -euo pipefail

cd "$(dirname "$0")"
# Both targets ship the Asgard/ folder, the same tree its release zip holds: since Oct 2026 Odin is an
# Asgard app (Asgard/apps/odin) and imports asgard.muninn, so it can't be shipped on its own. Terraform,
# steering files and docs have no business on the test host.
#   Odin (default):                  Asgard/apps/odin/tools/Invoke-WindowsChecks.ps1 under 5.1
#   Asgard (`run-checks.sh asgard`): Asgard/tools/windows_checks.py
TARGET="odin"
if [ "${1:-}" = "asgard" ]; then
  TARGET="asgard"
  shift
fi
APP_DIR="$(cd ../../Asgard && pwd)"

INSTANCE_ID="$(terraform output -raw instance_id)"
BUCKET="$(terraform output -raw bucket)"
REGION="$(terraform output -raw region)"
EXTRA_ARGS="${*:-}"

echo "==> instance $INSTANCE_ID in $REGION, bucket $BUCKET"

# ---- 1. Wait until SSM can actually reach the host -----------------------------------------
echo "==> waiting for the SSM agent to register (first boot installs Python, so allow a few minutes)"
for _ in $(seq 1 60); do
  STATUS="$(aws ssm describe-instance-information --region "$REGION" \
    --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || echo "None")"
  if [ "$STATUS" = "Online" ]; then
    echo "    agent online"
    break
  fi
  sleep 10
done
if [ "${STATUS:-None}" != "Online" ]; then
  echo "SSM agent never came online. Check the instance in the console, then retry." >&2
  exit 1
fi

# ---- 2. Push the checkout up ----------------------------------------------------------------
# Secrets and local state stay here: the token file, the sqlite state and real calendar exports are
# excluded. Terraform state (which holds the lab password) is outside Asgard/ and never in scope.
echo "==> syncing $APP_DIR to s3://$BUCKET/$TARGET"
aws s3 sync "$APP_DIR" "s3://$BUCKET/$TARGET" \
  --region "$REGION" \
  --delete \
  --only-show-errors \
  --exclude '*/__pycache__/*' \
  --exclude '*.pyc' \
  --exclude '*.dpapi' \
  --exclude '*state.db' \
  --exclude 'exports/*' \
  --exclude 'logs/*'

# ---- 3. Run the checks over SSM -------------------------------------------------------------
# powershell.exe is Windows PowerShell 5.1, which is the whole point. There is deliberately no
# -ExecutionPolicy Bypass: files arriving via `aws s3 sync` carry no zone marking, so the
# default RemoteSigned policy runs them. If this step fails on policy, that is a real finding.
AWS_EXE='C:\Program Files\Amazon\AWSCLIV2\aws.exe'
PARAMS_FILE="$(mktemp -t m2j-ssm-params)"
trap 'rm -f "$PARAMS_FILE"' EXIT

python3 - "$PARAMS_FILE" "$BUCKET" "$AWS_EXE" "$EXTRA_ARGS" "$TARGET" <<'PY'
import json
import sys

params_file, bucket, aws_exe, extra_args, target = sys.argv[1:6]
local = "C:\\m2j\\repo" if target == "odin" else "C:\\m2j\\asgard"
if target == "odin":
    run = ("powershell.exe -NoProfile -File "
           "C:\\m2j\\repo\\apps\\odin\\tools\\Invoke-WindowsChecks.ps1 %s" % extra_args)
else:
    # py.exe is the all-users launcher in C:\Windows, so it is on the SSM agent's PATH even though the
    # agent started before Python was installed. -X utf8 keeps the transcript readable in S3.
    run = "py -3 -X utf8 C:\\m2j\\asgard\\tools\\windows_checks.py %s" % extra_args

commands = [
    "$ErrorActionPreference = 'Continue'",
    "if (-not (Test-Path C:\\m2j\\bootstrap-complete.txt)) {",
    "  Write-Host 'First-boot bootstrap has not finished. See C:\\m2j\\bootstrap.log'",
    "  exit 1",
    "}",
    "& '%s' s3 sync s3://%s/%s %s --delete --only-show-errors" % (aws_exe, bucket, target, local),
    "if ($LASTEXITCODE -ne 0) { Write-Host 'repo sync failed'; exit 1 }",
    run,
    "exit $LASTEXITCODE",
]
with open(params_file, "w") as fh:
    json.dump({"commands": commands, "executionTimeout": ["3600"]}, fh)
PY

echo "==> running the $TARGET checks on the host"
COMMAND_ID="$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunPowerShellScript \
  --comment "$TARGET Windows checks" \
  --output-s3-bucket-name "$BUCKET" \
  --output-s3-key-prefix "check-output" \
  --parameters "file://$PARAMS_FILE" \
  --query 'Command.CommandId' --output text)"

echo "    command $COMMAND_ID"
# `aws ssm wait command-executed` gives up after 100 seconds; the Asgard checks take far longer.
STATUS="Pending"
for _ in $(seq 1 720); do
  STATUS="$(aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE_ID" --query 'Status' --output text 2>/dev/null || echo Pending)"
  case "$STATUS" in Pending|InProgress|Delayed) sleep 5 ;; *) break ;; esac
done

# Inline output is capped at 24 KB, so the full transcript comes from S3.
LOG="$(mktemp -t "$TARGET-checks")"
if aws s3 cp --region "$REGION" --only-show-errors \
    "s3://$BUCKET/check-output/$COMMAND_ID/$INSTANCE_ID/awsrunPowerShellScript/0.awsrunPowerShellScript/stdout" \
    "$LOG" 2>/dev/null; then
  cat "$LOG"
  echo "==> full transcript: $LOG"
else
  aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE_ID" --query 'StandardOutputContent' --output text
fi
STDERR="$(aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" \
  --instance-id "$INSTANCE_ID" --query 'StandardErrorContent' --output text)"

if [ -n "$STDERR" ] && [ "$STDERR" != "None" ]; then
  echo "---- stderr ----" >&2
  echo "$STDERR" >&2
fi

echo ""
echo "==> status: $STATUS"
echo "==> full log: aws s3 ls s3://$BUCKET/check-output/$COMMAND_ID/ --recursive"

if [ "$STATUS" != "Success" ]; then
  exit 1
fi
