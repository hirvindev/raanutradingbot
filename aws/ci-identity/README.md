# One-time bootstrap: OIDC trust for GitHub Actions

Run these once, locally, with your own AWS credentials. Never run this
template from CI — it's the trust boundary CI depends on, so CI must not be
able to change it.

**Order matters**: `cdk bootstrap` first, this template second — the
template's permission policy references the deploy-role and
file-publishing-role ARNs that `cdk bootstrap` creates, using its default,
predictable naming.

## 1. Bootstrap CDK (if you haven't already for this account/region)

```
npx cdk bootstrap aws://<YOUR_ACCOUNT_ID>/eu-central-1
```

This creates the `CDKToolkit` CloudFormation stack: a staging bucket plus
four IAM roles (deploy, file-publishing, image-publishing, lookup). You did
not write these and shouldn't need to touch them again.

## 2. Check for an existing GitHub OIDC provider

```
aws iam list-open-id-connect-providers
```

AWS allows only **one** OIDC provider per URL per account. If
`token.actions.githubusercontent.com` is already listed (from an earlier,
unrelated project), copy its ARN — you'll pass it in below instead of
letting this template create a duplicate.

## 3. Deploy the identity role

```
aws cloudformation deploy \
  --template-file aws/ci-identity/github-oidc-role.yaml \
  --stack-name raanutradingbot-github-oidc \
  --capabilities CAPABILITY_NAMED_IAM \
  --region eu-central-1 \
  --parameter-overrides \
      GitHubOrg=hirvindev \
      GitHubRepo=raanutradingbot \
      GitHubBranch=main
      # add ExistingOidcProviderArn=<arn> here if step 2 found one
```

## 4. Wire the output into GitHub

```
aws cloudformation describe-stacks \
  --stack-name raanutradingbot-github-oidc \
  --query "Stacks[0].Outputs[0].OutputValue" --output text
```

Take that ARN and add it to the GitHub repo as **Settings → Secrets and
variables → Actions → Variables** (not Secrets — it isn't sensitive) named
`AWS_DEPLOY_ROLE_ARN`. The workflow at
`.github/workflows/deploy-aws.yml` reads it from there.

## Before trusting any of this, verify the thumbprint

`github-oidc-role.yaml` has a comment flagging that its `ThumbprintList`
value may be stale — GitHub's OIDC certificate chain has rotated before, and
AWS has changed how strictly it enforces manual thumbprint pinning for
well-known providers. Check current AWS/GitHub guidance before running step
3 for the first time.
