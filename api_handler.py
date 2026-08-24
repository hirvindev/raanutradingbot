"""
api_handler.py — AWS Lambda entrypoint wrapping the FastAPI app via Mangum
============================================================================
Deployed behind a Lambda Function URL, itself behind CloudFront's /api/* and
/webhook/* behaviors — see aws/stacks/skeleton_stack.py. Never imported by
server.py or run on Railway.

lifespan="off" is deliberate, not an oversight: server.py's own `lifespan`
starts three background asyncio loops (pre-market scan, scheduled trades,
profit monitor). Mangum's default lifespan="auto" would try to run that
startup hook on every cold start, spawning loops that make no sense inside a
request/response Lambda and would leak across warm-container reuse. Those
three responsibilities instead run in worker_handler.py, invoked on its own
schedule.
"""

from lambda_secrets import load_ssm_secrets

load_ssm_secrets()

from mangum import Mangum

from server import app

handler = Mangum(app, lifespan="off")
