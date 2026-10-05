#!/usr/bin/env bash
#
# Run one PowerShell command on the lab VM over SSM and print its output. For looking around
# while debugging a check; the checks themselves go through run-checks.sh.
#
# Usage:  ./remote.sh 'Get-Content C:\m2j\bootstrap.log -Tail 20'
#
# Output is SSM's inline output, capped at 24 KB. The command runs as SYSTEM.

set -euo pipefail
cd "$(dirname "$0")"
INSTANCE_ID="$(terraform output -raw instance_id)"
REGION="$(terraform output -raw region)"
PARAMS_FILE="$(mktemp -t m2j-remote)"
trap 'rm -f "$PARAMS_FILE"' EXIT
python3 -c 'import json, sys; json.dump({"commands": [sys.argv[2]], "executionTimeout": ["1800"]}, open(sys.argv[1], "w"))' \
  "$PARAMS_FILE" "$1"
COMMAND_ID="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunPowerShellScript --parameters "file://$PARAMS_FILE" \
  --query 'Command.CommandId' --output text)"
STATUS="Pending"
for _ in $(seq 1 360); do
  STATUS="$(aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE_ID" --query 'Status' --output text 2>/dev/null || echo Pending)"
  case "$STATUS" in Pending|InProgress|Delayed) sleep 3 ;; *) break ;; esac
done
aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" \
  --query '[StandardOutputContent, StandardErrorContent]' --output text
echo "==> $STATUS"
