# aws/ — AWS deployment (Phase 2, isolated from the live app)

This folder is a from-scratch AWS deployment for RaanuTradingBot, built up
step by step. `server.py` and everything else at the repo root keeps
running on Railway exactly as before while this is built and tested —
nothing here touches Railway.

**Phase 1 (done): prove the pipeline.** A static page in S3 behind
CloudFront, calling one "hello world" Lambda through a Function URL,
deployed by GitHub Actions with zero stored AWS credentials. That
skeleton's job was only to prove the deploy mechanics; it's now replaced.

**Phase 2 (this phase): the real bot.** The dashboard (`RaanuTradingBot.html`
and its assets) now serves from S3/CloudFront for real. The FastAPI app is
wrapped by Mangum behind a second Lambda (`api_handler.py`), reachable
through the *same* CloudFront distribution at `/api/*` and `/webhook/*` — so
the browser only ever sees one origin and the dashboard's
`const API = location.origin` needed zero changes. A worker Lambda
(`worker_handler.py`) stands in for `server.py`'s three background
`asyncio` loops (pre-market scan, scheduled trade slots, profit monitor) —
Lambda has no persistent process to run them in, so it's invoked every 5
minutes by an EventBridge rule and decides for itself what's due, reusing
the exact DST-aware ET scheduling logic `server.py` already has. The local
JSON state files (`trades_log.json`, `position_peaks.json`, etc.) are
replaced by one DynamoDB table when running on Lambda — see `datadir.py`'s
`state_load`/`state_save`, which fall back to the original files everywhere
else (Railway, local dev) unchanged.

**The worker's EventBridge trigger deploys DISABLED.** It can be invoked
manually for testing, but no autonomous scheduled scan/trade happens from
AWS until you explicitly enable it — Railway's own scheduler keeps running
throughout, and both hitting the same paper account/weekly limits at once
would double-count trades. Flip it on only once you're ready to retire
Railway's scheduler (or accept the overlap deliberately).

**Not done yet:** migrating Railway's actual trade history into DynamoDB
(this deploys with empty state, which is safe exactly because the scheduler
starts disabled), and true SSE streaming for `/api/scan/stream` (Lambda
buffers the full response before returning it, so the live per-25-ticker
progress ticks degrade to a blocking wait — the scan itself still finishes
in a few seconds per `scanner.py`'s own numbers).

## Layout

```
aws/
├── app.py                  CDK app entrypoint
├── cdk.json                CDK CLI config
├── requirements.txt        Python deps for the CDK app itself (not the bot)
├── package.json            pins the CDK CLI version (npx cdk ...)
├── stacks/
│   └── skeleton_stack.py   the whole stack: S3, CloudFront, DynamoDB, both
│                           Lambdas, the EventBridge rule, IAM grants
├── site/                   GENERATED at synth time from RaanuTradingBot.html
│                           /sw.js/manifest.webmanifest/icons — gitignored,
│                           never hand-edit; see skeleton_stack.py's
│                           _stage_dashboard_site()
└── ci-identity/
    ├── github-oidc-role.yaml   one-time bootstrap: OIDC provider + CI role
    └── README.md                exact commands to run it, once, by hand

Repo root (Phase 2 is where the "isolated aws/ folder" boundary from Phase 1
ends — porting the bot inherently means touching its actual code):
├── Dockerfile.lambda   both Lambdas' image. Named ".lambda", not "Dockerfile"
│                       — Railway auto-detects a bare "Dockerfile" and would
│                       switch its own build mechanism away from Procfile.
├── .dockerignore       keeps the image to exactly the app — no .env, no
│                       keystores, no git history.
├── api_handler.py      Mangum(app, lifespan="off") — see its own docstring
│                       for why lifespan="off" matters.
├── worker_handler.py   the 5-minute dispatcher described above.
└── lambda_secrets.py   loads SSM Parameter Store into os.environ at cold
                        start, so every existing os.getenv(...) call site
                        needed zero changes.
```

## Why these choices (the parts that aren't visible from the code alone)

- **CDK, not SAM.** SAM was the first choice and it isn't actually
  restricted to Lambda — a SAM template is CloudFormation, so it can define
  S3/CloudFront/DynamoDB fine. The switch happened once "the stack is more
  than one Lambda" became the real requirement: CDK's `S3BucketOrigin`,
  `BucketDeployment`, and `add_function_url` constructs collapse boilerplate
  that raw CloudFormation/SAM makes you hand-write (the OAC bucket policy
  especially), and it keeps the infrastructure code in the same language as
  everything else in this repo.
- **Two-role CI delegation, via CDK's own roles.** The GitHub OIDC identity
  role has exactly one permission: `sts:AssumeRole` on the CDK bootstrap's
  `deploy-role` and `file-publishing-role` — nothing else. It never touches
  S3/CloudFront/Lambda directly; that happens through CDK's separate
  `cfn-exec-role`, which only the CloudFormation service can assume. This
  wasn't hand-rolled — `cdk bootstrap` already provisions this split, we
  just built the identity role to delegate into it rather than granting
  broad permissions directly. Chosen over a single do-everything role
  because this repo is public: if the CI side is ever compromised, it can
  still do almost nothing on its own.
- **Lambda Function URL, not API Gateway**, for the same reason repeated
  through this whole design: no extra AWS service billed on its own meter,
  and `server.py`'s eventual auth (ASGI middleware) makes API Gateway's
  authorizer/usage-plan features redundant rather than useful.
- **Dev container, not host-installed tooling.** Node/AWS CLI/`gh` live in
  `.devcontainer/`, not on the Mac. The CDK CLI specifically is *not* part
  of the container image — it's pinned in `aws/package.json` so `npx cdk`
  always matches the `aws-cdk-lib` version in `requirements.txt`, on any
  machine that opens this repo.

## Dev environment

This is developed inside a **dev container** (`.devcontainer/`) — Node.js,
AWS CLI, GitHub CLI, and (from Phase 2 on) Docker live in the container, not
on your host. The CDK CLI itself is *not* baked into the container image;
it's pinned in `aws/package.json` and provisioned via `npm install` so the
version you run locally can never drift from the version CI runs.

1. On the host, once: `aws configure` (or SSO login) so `~/.aws` exists —
   the container mounts it live, it never copies or bakes in credentials.
2. VS Code → **Reopen in Container** (or **Rebuild Container** if you
   already had one from before Phase 2 — the `docker-in-docker` feature is
   new and won't be present until you rebuild). `postCreateCommand` runs
   automatically and installs `aws/requirements.txt` + the pinned CDK CLI.
3. Inside the container, confirm it can see your credentials and Docker:
   ```
   aws sts get-caller-identity
   docker version
   ```
   Docker is needed because `cdk deploy`'s asset-publishing step builds
   `Dockerfile.lambda` locally — `cdk synth` alone does not need it (CDK
   computes the template from source, and only publishes/builds the image
   when you actually deploy). GitHub Actions runners already have Docker
   preinstalled, so CI needs no changes for this.

Not using VS Code / dev containers? Install Node.js + AWS CLI on the host
yourself, then `cd aws && python3 -m venv .venv && source .venv/bin/activate
&& pip install -r requirements.txt && npm install` — same result, just
without the isolation.

## One-time AWS setup

Run once, from inside the dev container, with your own credentials — this
is the only part that touches your real AWS account, and it never runs
from CI.

1. Bootstrap CDK in your account/region (one-time per account+region):
   ```
   npx cdk bootstrap aws://<YOUR_ACCOUNT_ID>/eu-central-1
   ```
2. Follow `aws/ci-identity/README.md` to create the GitHub OIDC trust and
   the CI identity role — this is the piece that lets GitHub Actions deploy
   without ever holding an AWS access key.
3. Seed the secrets Lambda reads at cold start (`lambda_secrets.py`) into
   SSM Parameter Store — CDK only grants the Lambdas read access to this
   path, it never creates or touches the values themselves:
   ```
   aws ssm put-parameter --name /raanutradingbot/ALPACA_API_KEY     --type SecureString --value "<...>"
   aws ssm put-parameter --name /raanutradingbot/ALPACA_SECRET_KEY  --type SecureString --value "<...>"
   aws ssm put-parameter --name /raanutradingbot/API_READ_TOKEN     --type SecureString --value "<...>"
   aws ssm put-parameter --name /raanutradingbot/TRADE_PIN          --type SecureString --value "<...>"
   # ...and any of TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID*, TWILIO_ACCOUNT_SID,
   # TWILIO_AUTH_TOKEN, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY,
   # FCM_SERVICE_ACCOUNT_JSON, USER_WHATSAPP, TWILIO_WHATSAPP_FROM you want
   # working — an unset one just means that feature silently no-ops, same
   # as an unset .env value does today.
   ```
   Plain tuning config (`STOP_ATR_MULT`, `WEEKLY_TRADE_LIMIT`, etc.) is
   **not** here — it's set directly as Lambda environment variables in
   `skeleton_stack.py`, same as it's a plain `.env` entry on Railway.

## Deploying locally (do this before ever trusting CI with it)

```
cd aws
npx cdk synth        # sanity-check the template — no AWS calls, no Docker build
npx cdk diff          # should show only new resources on a clean account
npx cdk deploy        # this is the step that actually builds Dockerfile.lambda
```

Then check the things that actually prove this works:
- `curl -I https://<bucket-name>.s3.<region>.amazonaws.com/index.html` →
  expect **403** (proves the bucket is private, CloudFront is the only door in)
- Open the `CloudFrontURL` stack output in a browser → the real dashboard
  should load and show real (or empty-but-not-erroring) account data
- `curl https://<CloudFrontURL>/api/health` → JSON health response, routed
  through CloudFront to the API Lambda
- `curl <ApiFunctionUrl stack output>/api/health` → the same thing, direct
  to the Lambda, bypassing CloudFront entirely
- The `WorkerFunction` in the Lambda console can be invoked manually (any
  test event) to prove it runs end to end — its EventBridge trigger deploys
  **disabled**, so nothing fires on a schedule until you explicitly enable it

Only once all three pass, trigger `.github/workflows/deploy-aws.yml` from
the Actions tab and confirm it produces the same result using the OIDC role
instead of your local credentials.
