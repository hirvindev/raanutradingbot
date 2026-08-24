# aws/ — AWS deployment (in progress, isolated from the live app)

This folder is a from-scratch AWS deployment for RaanuTradingBot, built up
step by step. It does not touch, import, or depend on anything outside this
folder — `server.py` and everything else at the repo root keeps running on
Railway exactly as before while this is built and tested.

**Phase 1 (this phase): prove the pipeline.** A static page in S3, served
through CloudFront, calling one "hello world" Lambda through a Function URL.
No trading logic, no secrets, nothing that touches Alpaca/Twilio/Telegram.
The only thing being validated here is: can GitHub Actions deploy to this
AWS account with zero stored credentials, and does the resulting
S3 → CloudFront → Lambda path actually work end to end.

**Later phases** (not yet started): port `strategy.py`/`scanner.py`/
`auto_trader.py`/`profit_monitor.py` etc. into a real `worker` Lambda
triggered by EventBridge Scheduler, wrap `server.py` behind a second Lambda
via Mangum, replace the local JSON state files with DynamoDB. None of that
exists yet — this folder is deliberately just the skeleton.

## Layout

```
aws/
├── app.py                  CDK app entrypoint
├── cdk.json                CDK CLI config
├── requirements.txt        Python deps for the CDK app itself (not the bot)
├── package.json            pins the CDK CLI version (npx cdk ...)
├── stacks/
│   └── skeleton_stack.py   the whole phase-1 stack: S3, CloudFront, Lambda
├── lambda/hello/handler.py the "hello world" Lambda — stdlib only
├── site/index.html         the static page, fetches the Lambda's response
└── ci-identity/
    ├── github-oidc-role.yaml   one-time bootstrap: OIDC provider + CI role
    └── README.md                exact commands to run it, once, by hand
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
AWS CLI, and GitHub CLI live in the container, not on your host. The CDK
CLI itself is *not* baked into the container image; it's pinned in
`aws/package.json` and provisioned via `npm install` so the version you run
locally can never drift from the version CI runs.

1. On the host, once: `aws configure` (or SSO login) so `~/.aws` exists —
   the container mounts it live, it never copies or bakes in credentials.
2. VS Code → **Reopen in Container**. `postCreateCommand` runs automatically
   and installs `aws/requirements.txt` + the pinned CDK CLI.
3. Inside the container, confirm it can see your credentials:
   ```
   aws sts get-caller-identity
   ```

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

## Deploying locally (do this before ever trusting CI with it)

```
cd aws
npx cdk synth        # sanity-check the template, no AWS calls
npx cdk diff          # should show only new resources on a clean account
npx cdk deploy
```

Then check the three things that actually prove this works:
- `curl -I https://<bucket-name>.s3.<region>.amazonaws.com/index.html` →
  expect **403** (proves the bucket is private, CloudFront is the only door in)
- Open the `CloudFrontURL` stack output in a browser → the page should load
  and show a JSON response from the Lambda, not an error
- `curl <FunctionUrl stack output>` → the Lambda's JSON directly, bypassing
  the browser entirely

Only once all three pass, trigger `.github/workflows/deploy-aws.yml` from
the Actions tab and confirm it produces the same result using the OIDC role
instead of your local credentials.
