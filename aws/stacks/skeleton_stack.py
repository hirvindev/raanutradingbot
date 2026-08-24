import shutil
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

AWS_DIR = Path(__file__).parent.parent
REPO_ROOT = AWS_DIR.parent

# Both Lambda images build for the same architecture regardless of which
# machine runs `cdk deploy` — without pinning this, a local build on an
# arm64 dev machine and a GitHub Actions build on an amd64 runner would
# silently produce different-architecture images. amd64 is the safer pin:
# GitHub Actions runners are natively amd64 (no QEMU/Buildx emulation
# needed there), and Docker Desktop's cross-build support for amd64 targets
# from an arm64 host is the common, well-supported direction.
LAMBDA_PLATFORM = ecr_assets.Platform.LINUX_AMD64
LAMBDA_ARCHITECTURE = lambda_.Architecture.X86_64


def _stage_dashboard_site(site_dir: Path) -> None:
    """Regenerate aws/site/ from the real dashboard files at synth time, so
    RaanuTradingBot.html stays the single source of truth for both Railway
    and AWS — aws/site/ itself is generated and gitignored, not hand-authored.

    An explicit allow-list, not "deploy the repo root minus some excludes":
    a deny-list risks a future new top-level file, or a typo in the excludes,
    landing on a public CDN. Only these four things make up the dashboard.
    """
    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "RaanuTradingBot.html", site_dir / "index.html")
    shutil.copy2(REPO_ROOT / "sw.js", site_dir / "sw.js")
    shutil.copy2(REPO_ROOT / "manifest.webmanifest", site_dir / "manifest.webmanifest")
    shutil.copytree(REPO_ROOT / "icons", site_dir / "icons")


class SkeletonStack(Stack):
    """Phase 1 proved the pipeline (S3+CloudFront+Lambda via OIDC, no stored
    AWS credentials). Phase 2 replaces the hello-world placeholder with the
    real bot: the dashboard on S3/CloudFront, the FastAPI app wrapped by
    Mangum behind a second Lambda (also reachable through CloudFront, so the
    browser only ever sees one origin and no CORS is needed), a worker
    Lambda standing in for server.py's three background loops (Lambda has
    no persistent process to run them in), and DynamoDB replacing the local
    JSON state files. See aws/README.md and CLAUDE.md's AWS Migration
    section for the full "why" behind each choice.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        _stage_dashboard_site(AWS_DIR / "site")

        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # One item per today's JSON state file (trades_log.json,
        # position_peaks.json, picks_log.json, push_subs.json,
        # push_native.json, notifications.json, last_picks*.json,
        # scheduler_marks.json), keyed by that same filename — see
        # datadir.py's state_load/state_save. RETAIN, not the site bucket's
        # DESTROY/auto-delete: this table will hold real trade history, and
        # a `cdk destroy` must not be able to erase it.
        state_table = dynamodb.Table(
            self,
            "StateTable",
            partition_key=dynamodb.Attribute(name="state_key", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
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

        # No ALLOWED_ORIGINS pointing at the distribution's own domain here —
        # that would make the Lambda depend on the Distribution's output
        # while the Distribution's behavior (below) depends on the Lambda's
        # Function URL, a genuine circular dependency CloudFormation refuses
        # to resolve. It isn't needed anyway: CloudFront makes the dashboard
        # and the API the same origin from the browser's perspective, so no
        # request here is ever actually cross-origin — the CORS check in
        # server.py's middleware simply never has anything to enforce.
        common_env = {
            "STATE_BACKEND": "dynamodb",
            "STATE_TABLE": state_table.table_name,
        }

        # Both Lambdas share one built image (same Dockerfile, same repo-root
        # build context) — CDK/Docker layer caching means this only builds
        # once even though two DockerImageFunctions reference it.
        api_image = lambda_.DockerImageCode.from_image_asset(
            str(REPO_ROOT),
            file="Dockerfile.lambda",
            platform=LAMBDA_PLATFORM,
            cmd=["api_handler.handler"],
        )
        worker_image = lambda_.DockerImageCode.from_image_asset(
            str(REPO_ROOT),
            file="Dockerfile.lambda",
            platform=LAMBDA_PLATFORM,
            cmd=["worker_handler.handler"],
        )

        api_fn = lambda_.DockerImageFunction(
            self,
            "ApiFunction",
            code=api_image,
            architecture=LAMBDA_ARCHITECTURE,
            timeout=Duration.seconds(60),
            memory_size=1024,
            environment=common_env,
        )

        worker_fn = lambda_.DockerImageFunction(
            self,
            "WorkerFunction",
            code=worker_image,
            architecture=LAMBDA_ARCHITECTURE,
            timeout=Duration.minutes(5),
            memory_size=1024,
            environment=common_env,
        )

        state_table.grant_read_write_data(api_fn)
        state_table.grant_read_write_data(worker_fn)

        # SSM parameters under /raanutradingbot/* are seeded by hand (see
        # aws/README.md) — CDK never creates or touches the values
        # themselves, only grants read access to this path. WithDecryption
        # needs kms:Decrypt on the default SSM key too.
        ssm_read = iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/raanutradingbot/*"],
        )
        ssm_decrypt = iam.PolicyStatement(
            actions=["kms:Decrypt"],
            resources=[f"arn:aws:kms:{self.region}:{self.account}:alias/aws/ssm"],
        )
        for fn in (api_fn, worker_fn):
            fn.add_to_role_policy(ssm_read)
            fn.add_to_role_policy(ssm_decrypt)

        # NONE, not IAM — the app's own API_READ_TOKEN/TRADE_PIN middleware
        # is the real auth (see server.py), matching the security model this
        # bot already uses on Railway. CORS here is only for directly
        # curl/browser-testing the Function URL itself — it's irrelevant to
        # the real dashboard traffic, which goes through CloudFront below
        # and is never actually cross-origin from the browser's perspective,
        # so no browser CORS check ever applies to it either way. Can't
        # include the CloudFront domain here even for convenience: that
        # would make the Function URL depend on the Distribution's output
        # while the Distribution depends on this Function URL as an origin
        # (added below) — the same circular dependency the dropped
        # ALLOWED_ORIGINS env var caused, CloudFormation can't resolve it.
        api_fn_url = api_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=[
                    "http://localhost:8000",
                    "http://127.0.0.1:8000",
                ],
                allowed_methods=[lambda_.HttpMethod.ALL],
                allowed_headers=["*"],
            ),
        )

        # /api/* and /webhook/* go to the Lambda; everything else (the
        # dashboard's static assets) falls through to the default S3
        # behavior above. One CloudFront domain for both — the dashboard's
        # `const API = location.origin` keeps working completely unchanged.
        api_origin = origins.FunctionUrlOrigin(api_fn_url)
        for path_pattern in ("/api/*", "/webhook/*"):
            distribution.add_behavior(
                path_pattern,
                api_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            )

        # Runs every 5 minutes; the worker itself decides what's actually
        # due (see worker_handler.py) using the same DST-aware ET logic
        # server.py already has, rather than pushing timezone math into a
        # UTC-only cron expression. Deploys DISABLED — confirmed with the
        # user: no autonomous scheduled scan/trade from AWS until this is
        # explicitly enabled, so it never races Railway's own scheduler
        # against the same paper account and weekly limits.
        events.Rule(
            self,
            "WorkerSchedule",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[events_targets.LambdaFunction(worker_fn)],
            enabled=False,
        )

        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[s3deploy.Source.asset(str(AWS_DIR / "site"))],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "CloudFrontURL", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "ApiFunctionUrl", value=api_fn_url.url)
        CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "StateTableName", value=state_table.table_name)
