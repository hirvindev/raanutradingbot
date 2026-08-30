"""
lambda_secrets.py — loads secrets from SSM Parameter Store into os.environ
============================================================================
Only used by the Lambda entrypoints (api_handler.py, worker_handler.py),
never by server.py itself — Railway/local dev keep reading .env exactly as
before. Must run BEFORE `server` (or anything it imports) is imported, so
every existing os.getenv(...) call site across the codebase picks up the
real value without any changes.

Only genuinely secret values live in SSM — plain tuning knobs (STOP_ATR_MULT,
WEEKLY_TRADE_LIMIT, etc.) are set directly as Lambda environment variables in
the CDK stack, same as they're plain .env entries on Railway.
"""

import logging
import os

log = logging.getLogger("raanu.lambda_secrets")

DEFAULT_PREFIX = "/raanutradingbot/"


def load_ssm_secrets(prefix: str = DEFAULT_PREFIX) -> int:
    """Populate os.environ from every SSM parameter under `prefix`.

    A parameter named "/raanutradingbot/ALPACA_API_KEY" becomes the env var
    ALPACA_API_KEY. Never overwrites a value already set in the environment
    (e.g. plain config set directly on the Lambda), mirroring load_dotenv's
    own override=False. Returns the number of variables set — 0 if the
    prefix has no parameters yet, which is harmless during early setup.

    Never raises. SSM being unreachable — no credentials locally, a
    throttle, a permissions gap — must not stop the process from starting:
    an app that boots and reports itself unconfigured is far easier to
    diagnose than a Lambda that dies during init with an import traceback.
    Features whose secrets are missing already no-op individually.
    """
    try:
        import boto3

        ssm = boto3.client("ssm")
        count = 0
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
            for param in page.get("Parameters", []):
                name = param["Name"].removeprefix(prefix)
                if name and name not in os.environ:
                    os.environ[name] = param["Value"]
                    count += 1
        log.info(f"Loaded {count} secret(s) from SSM Parameter Store ({prefix})")
        return count
    except Exception as e:
        log.warning(f"Could not load secrets from SSM ({prefix}): {e}")
        return 0
