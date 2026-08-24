from pathlib import Path

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

AWS_DIR = Path(__file__).parent.parent


class SkeletonStack(Stack):
    """Phase 1: prove the pipeline works, nothing more.

    A private S3 bucket behind CloudFront (Origin Access Control, not the
    legacy public-website-endpoint pattern), one dependency-free Lambda
    behind a Function URL, and the static page's own JS calling it. No
    trading logic and no application secrets belong in this stack — see
    aws/README.md for what comes later.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # Disposable by design for a skeleton phase — no data of any
            # value ever lives here. Revisit once this stack holds anything
            # you'd mind losing to an accidental `cdk destroy`.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        distribution = cloudfront.Distribution(
            self,
            "SiteDistribution",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )

        hello_fn = lambda_.Function(
            self,
            "HelloFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(AWS_DIR / "lambda" / "hello")),
        )

        # Function URL CORS answers the OPTIONS preflight AND stamps the
        # response headers on the real request automatically — the handler
        # itself needs zero CORS logic. Scoped to the real CloudFront domain
        # (no circular dependency: the URL's CORS config can reference the
        # distribution's domain within the same stack) plus localhost, for
        # testing the static page against a live deploy without CloudFront
        # in the loop. The response carries no sensitive data, so this is a
        # low-stakes place to keep things convenient.
        fn_url = hello_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=[
                    f"https://{distribution.distribution_domain_name}",
                    "http://localhost:8000",
                    "http://127.0.0.1:8000",
                ],
                allowed_methods=[lambda_.HttpMethod.GET],
            ),
        )

        # Uploads site/ into the bucket AND invalidates CloudFront as part
        # of `cdk deploy` — no separate `aws s3 sync` / `create-invalidation`
        # step needed, locally or in CI. config.json is synthesized here,
        # not committed — the page's own Function URL isn't a secret, but
        # there's no reason to hand-copy it either.
        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[
                s3deploy.Source.asset(str(AWS_DIR / "site")),
                s3deploy.Source.json_data("config.json", {"apiUrl": fn_url.url}),
            ],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "CloudFrontURL", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "FunctionUrl", value=fn_url.url)
        CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
