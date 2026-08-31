#!/usr/bin/env bash
#
# seed-secrets.sh — put the real credentials into SSM Parameter Store
# ====================================================================
# The obvious command leaks the secret twice:
#
#     aws ssm put-parameter --name /raanutradingbot/API_READ_TOKEN \
#                           --type SecureString --value "hunter2"
#
#   1. it lands in ~/.bash_history / ~/.zsh_history in plain text, and
#   2. while it runs it is an argv entry, so any other process on the box
#      can read it out of `ps auxww`.
#
# This script avoids both. Secrets are read from the terminal with echo off,
# held only in shell variables, and handed to the AWS CLI over **stdin** as
# --cli-input-json — never as an argument, never through a file on disk.
#
#     ./aws/seed-secrets.sh                # prompts for each value
#     ./aws/seed-secrets.sh API_READ_TOKEN # just one
#
# Nothing here ever prints a secret back, including on success.

set -euo pipefail

PROFILE="${AWS_PROFILE:-raanu-cdk}"
REGION="${AWS_REGION:-eu-central-1}"
PREFIX="/raanutradingbot"

# Everything the application reads from SSM. Keep in step with raanu/config.py;
# plain tuning knobs (STOP_ATR_MULT, WEEKLY_TRADE_LIMIT, ...) belong in the CDK
# stack's Lambda environment instead — only genuine secrets go here.
ALL_KEYS=(
  API_READ_TOKEN      # dashboard passphrase   — gates every /api/** request
  TRADE_PIN           # second secret          — gates every non-GET
  ALPACA_API_KEY      # broker key id
  ALPACA_SECRET_KEY   # broker secret
  TELEGRAM_BOT_TOKEN  # alerts (optional)
  TELEGRAM_CHAT_ID    # alerts (optional)
  VAPID_PUBLIC_KEY    # browser web push (optional)
  VAPID_PRIVATE_KEY   # browser web push (optional)
)

die() { printf '\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

command -v aws >/dev/null || die "aws CLI not found"
aws sts get-caller-identity --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1 \
  || die "profile '$PROFILE' cannot authenticate — check ~/.aws/credentials"

# jq would be the obvious way to build the request body, but that means
# shell-quoting the secret into an argument. So the value travels on **stdin**
# the whole way — into python, then into the AWS CLI:
#
#   * argv is world-readable through `ps auxww`, to every user on the box;
#   * the environment is not much better — /proc/<pid>/environ is readable by
#     anything running as the same user, which is everything you run;
#   * stdin is neither, and `printf` is a shell builtin so it forks nothing.
#
# python also does the JSON escaping, which matters: a PEM body or a base64
# blob will contain characters that would otherwise need quoting decisions.
put_parameter() {
  local name="$1" value="$2"
  printf '%s' "$value" \
  | RAANU_SEED_NAME="$PREFIX/$name" python3 -c 'import json,os,sys; json.dump({
      "Name": os.environ["RAANU_SEED_NAME"],
      "Value": sys.stdin.read(),
      "Type": "SecureString",
      "Overwrite": True,
  }, sys.stdout)' \
  | aws ssm put-parameter \
      --cli-input-json file:///dev/stdin \
      --profile "$PROFILE" --region "$REGION" >/dev/null
}

exists() {
  aws ssm get-parameter --name "$PREFIX/$1" --profile "$PROFILE" --region "$REGION" \
    --query 'Parameter.Name' --output text >/dev/null 2>&1
}

seed_one() {
  local name="$1" value="" confirm="" state="not set"
  exists "$name" && state="already set"

  printf '\n\033[1m%s\033[0m  (%s)\n' "$name" "$state"
  # -s: no echo. -r: backslashes are literal, which matters for base64 blobs
  # and PEM bodies.
  read -rs -p "  value (blank to skip): " value < /dev/tty; echo
  [ -z "$value" ] && { printf '  skipped\n'; return; }

  # Typos in a write-only field are invisible until something fails hours
  # later, so both entries must agree.
  read -rs -p "  confirm: " confirm < /dev/tty; echo
  if [ "$value" != "$confirm" ]; then
    printf '\033[31m  mismatch — skipped\033[0m\n'; return
  fi

  # A pasted value that picked up surrounding whitespace authenticates
  # nowhere and looks correct in every dashboard. The app strips on read
  # (config.env_str), but storing it clean keeps the two in agreement.
  case "$value" in
    *[![:space:]]*) ;;
    *) printf '\033[31m  whitespace only — skipped\033[0m\n'; return ;;
  esac

  put_parameter "$name" "$value"
  printf '  \033[32mstored\033[0m (%s chars, SecureString)\n' "${#value}"
}

# Tested against $# rather than ${#KEYS[@]}: macOS still ships bash 3.2 as
# /bin/bash, where expanding an empty array under `set -u` is an error.
if [ $# -eq 0 ]; then KEYS=("${ALL_KEYS[@]}"); else KEYS=("$@"); fi

cat <<BANNER
Seeding SSM parameters under $PREFIX
  profile: $PROFILE      region: $REGION
Input is hidden. Nothing is written to disk or to shell history.

Need a strong API_READ_TOKEN? It is also the key the dashboard's session
cookies are signed with, so length genuinely matters here:
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
BANNER

for key in "${KEYS[@]}"; do seed_one "$key"; done

printf '\n\033[1mNow set:\033[0m\n'
aws ssm get-parameters-by-path --path "$PREFIX" --recursive \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Parameters[].Name' --output text 2>/dev/null \
  | tr '\t' '\n' | sed 's|^|  |' || printf '  (none)\n'

cat <<'NEXT'

Secrets are read at Lambda COLD START, so a running container keeps the old
values. Force new ones before testing:

  aws lambda update-function-configuration --function-name <ApiFunction> \
      --environment "Variables={SECRETS_ROTATED_AT=$(date +%s)}"

...or simply redeploy, or wait out the idle timeout.

API_READ_TOKEN is also the key the dashboard's session cookies are signed
with, so changing it signs everyone out. That is the revocation switch: if a
laptop goes missing, re-run this for API_READ_TOKEN alone.
NEXT
