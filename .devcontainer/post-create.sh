#!/usr/bin/env bash
# Runs once when the dev container is created. Sets up the aws/ CDK app
# only — this container is scoped to the AWS deployment work, not the
# Railway bot (its own requirements.txt is untouched here; install it
# yourself inside the container if you ever want to run the bot from here
# too).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Installing aws/ Python dependencies"
python3 -m pip install --user -r aws/requirements.txt

echo "==> Installing the pinned CDK CLI (aws/package.json) — run it via 'npx cdk', not a global install"
(cd aws && npm install)

cat <<'EOF'

Ready. From aws/:
  npx cdk --version
  aws sts get-caller-identity     # confirms the mounted ~/.aws credentials work in here
  npx cdk bootstrap aws://<account-id>/eu-central-1   # one-time, see aws/README.md
EOF
