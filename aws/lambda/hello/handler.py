import json


def lambda_handler(event, context):
    """Phase-1 skeleton handler — proves the Function URL is reachable and
    returns something real. No dependencies, no application logic."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "ok", "source": "lambda"}),
    }
