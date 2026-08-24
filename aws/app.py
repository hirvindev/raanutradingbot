import os

import aws_cdk as cdk

from stacks.skeleton_stack import SkeletonStack

app = cdk.App()

# Pinned explicitly (rather than left to CDK's default account/region
# lookup) so this stack never needs the CDK bootstrap's lookup-role — the
# CI identity role only ever needs to assume the deploy-role and
# file-publishing-role, nothing more.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "eu-central-1"),
)

SkeletonStack(app, "RaanuAwsSkeleton", env=env)

app.synth()
