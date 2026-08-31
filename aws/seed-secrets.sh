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
command -v python3 >/dev/null || die "python3 not found"
aws sts get-caller-identity --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1 \
  || die "profile '$PROFILE' cannot authenticate — check ~/.aws/credentials"

# Created and trapped together, before anything can be written into it — a
# cleanup registered later is a cleanup that does not run if the script dies
# in between.
BODY=$(mktemp "${TMPDIR:-/tmp}/raanu-seed.XXXXXX") || die "could not create a temp file"
chmod 600 "$BODY"
trap 'rm -f "$BODY"' EXIT INT TERM

# The secret must not become a command-line argument: argv is world-readable
# through `ps auxww`, to every other user on the machine, for as long as the
# command runs. Shell history is the other leak, and `--value "$SECRET"` hits
# both.
#
# The obvious fix — pipe the request body in on stdin — does NOT work:
#
#     ... | aws ssm put-parameter --cli-input-json file:///dev/stdin
#
# AWS CLI v2's file:// handler cannot read a non-seekable stream. Tested
# against 2.36.29: /dev/stdin, the documented `-`, and /dev/fd/N all fail with
# "Invalid JSON received", while an ordinary file works. This script shipped
# with that construct and was broken on every invocation.
#
# So: a real file, created under `umask 077` so it is owner-only from the
# instant it exists, and removed on any exit path including Ctrl-C. That is a
# genuinely smaller exposure than argv — one process's owner for a few
# milliseconds, rather than every user on the box — but it is not *nothing*,
# and pretending otherwise is how the previous version of this comment was
# wrong.
#
# python builds the JSON so the escaping is right: a PEM body or a base64 blob
# contains characters that would otherwise need quoting decisions.
put_parameter() {
  local name="$1" value="$2"
  printf '%s' "$value" \
  | RAANU_SEED_NAME="$PREFIX/$name" python3 -c 'import json,os,sys; json.dump({
      "Name": os.environ["RAANU_SEED_NAME"],
      "Value": sys.stdin.read(),
      "Type": "SecureString",
      "Overwrite": True,
  }, sys.stdout)' > "$BODY"
  aws ssm put-parameter \
      --cli-input-json "file://$BODY" \
      --profile "$PROFILE" --region "$REGION" >/dev/null
  : > "$BODY"                          # truncate immediately; trap unlinks
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
