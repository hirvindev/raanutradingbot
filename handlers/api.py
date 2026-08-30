"""
handlers.api — Lambda entrypoint for the HTTP API
==================================================
Thin by design: load secrets, build the app, hand it to Mangum.

``lifespan="off"`` is deliberate and load-bearing. The app's lifespan starts
four background loops (pre-market scan, trade slots, exit monitor, monthly
report) which only make sense in a persistent process. Mangum's default
``lifespan="auto"`` would run that on every cold start, spawning loops
inside a request/response Lambda that leak across warm-container reuse.
Those four jobs belong to the worker (see handlers/worker.py), which owns
the schedule.
"""

from raanu.secrets import load_ssm_secrets

# Before create_app(), so config reads see the real values. Config is lazy
# now, so this is belt-and-braces rather than the load-bearing ordering it
# used to be.
load_ssm_secrets()

from mangum import Mangum  # noqa: E402

from raanu.api.app import create_app  # noqa: E402

handler = Mangum(create_app(with_loops=False), lifespan="off")
