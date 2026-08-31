# Deploying

## There is only one deploy, and it is manual

```bash
git push                                        # does NOT deploy
gh workflow run "Deploy AWS skeleton" && gh run watch
```

⚠️ **Pushing to `main` deploys nothing.** `.github/workflows/deploy-aws.yml`
is `workflow_dispatch`-only, deliberately — this is still the validation phase
and a deploy should be a decision, not a side effect of a commit. The header
comment in that file has the two lines to uncomment when that changes.

One run deploys everything: the whole `raanu/` package, the Lambda handlers,
the CDK stack, `RaanuTradingBot.html`, `sw.js`, the manifest and the icons.
Both Lambdas share one container image, so the slow part is the Docker build.

CI authenticates through GitHub OIDC and holds no AWS access key — see
`aws/ci-identity/README.md`.

Check it landed:

```bash
curl -s https://d2c2x91kx43y5d.cloudfront.net/api/health | python3 -m json.tool
```

Give it a few minutes for the image build. `state.persistent` must be `true`
and `state.backend` must read `dynamodb`.

The dashboard is served from S3 with `Cache-Control: no-cache,
must-revalidate`, and the deploy invalidates CloudFront — so a hard refresh
picks up a change immediately. Without that header the browser's own heuristic
cache once held a fix back for hours, and no CloudFront invalidation can reach
a copy that is already on the client.

## Secrets

Never `aws ssm put-parameter --value "<secret>"` by hand — that lands in shell
history and in `ps auxww`. Use:

```bash
./aws/seed-secrets.sh                  # prompts for each, input hidden
./aws/seed-secrets.sh API_READ_TOKEN   # or just one
```

Secrets are read at Lambda **cold start**, so a running container keeps the old
values. Redeploy, or wait out the idle timeout, before testing a change.

## The PWA

There is no separate app release. The dashboard is a Progressive Web App:
Add to Home Screen on Android Chrome or iOS Safari (16.4+) gives an app icon,
no browser chrome, and web push. It updates whenever the dashboard does,
because it *is* the dashboard.

The native React Native app, the TWA wrapper, `deploy-mobile.sh` and
`tools/play_upload.py` were **deleted on 31 Aug 2026** — see the Pending
section of `CLAUDE.md` for why, and what you would have to settle before
bringing one back.

## What is deliberately NOT automated

**The EventBridge worker schedule ships DISABLED.** Nothing scans or trades
autonomously until it is explicitly enabled in the console.

**`ALPACA_MODE`.** Stays `paper`. No script touches it.

**Production trading.** No script flips anything to live.
