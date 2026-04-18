"""
Trailer webhook server — entrypoint.

Usage:
    DATABASE_URL=postgresql://user:pass@localhost/dbname \
    PYTHONPATH=. python -m services.trailer_webhook.server

Environment variables:
    DATABASE_URL        — Postgres connection string (required)
    REDIS_URL           — Redis connection string (required when auth enabled)
    WEBHOOK_HOST        — bind host (default: 0.0.0.0)
    WEBHOOK_PORT        — bind port (default: 8100)
    MODEL_PROFILE       — model profile for ingested jobs (default: default)
    PROMPT_VERSION      — prompt version for ingested jobs (default: v1)
    PANOPTIC_SHARED_SECRET_ACTIVE / _PREVIOUS — HMAC auth secrets
    PANOPTIC_DEV_MODE / PANOPTIC_AUTH_ENABLED — dev-disable hatch
"""

from __future__ import annotations

import logging
import os

import uvicorn
from sqlalchemy import create_engine

from services.trailer_webhook.app import create_app
from shared.auth.hmac_auth import (
    AUTH_ENABLED,
    assert_config_sane,
    start_dev_warning_loop,
)
from shared.health.probes import start_probe_loop
from shared.health.state import HealthState
from shared.utils.leases import generate_worker_id
from shared.utils.redis_client import get_redis_client
from shared.utils.streams import bootstrap_streams

log = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")
REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379")
WEBHOOK_HOST: str = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = int(os.environ.get("WEBHOOK_PORT", "8100"))
MODEL_PROFILE: str = os.environ.get("MODEL_PROFILE", "default")
PROMPT_VERSION: str = os.environ.get("PROMPT_VERSION", "v1")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Fail fast on a misconfigured auth setup (e.g. auth on but no secret).
    assert_config_sane()

    # If auth is disabled via the dev-mode hatch, start the loud warning loop.
    start_dev_warning_loop()

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()
    bootstrap_streams(r)

    # Health state + background probe loop.
    health = HealthState(service_name="trailer_webhook", worker_id=generate_worker_id())
    health.mark_critical("postgres", "redis")
    start_probe_loop(
        health,
        targets={
            "postgres": {"database_url": DATABASE_URL},
            "redis": {"redis_url": REDIS_URL},
        },
    )

    app = create_app(
        engine,
        r,
        model_profile=MODEL_PROFILE,
        prompt_version=PROMPT_VERSION,
        health_state=health,
        database_url=DATABASE_URL,
        redis_url=REDIS_URL,
    )

    log.info(
        "starting webhook server host=%s port=%d model=%s prompt=%s auth_enabled=%s",
        WEBHOOK_HOST, WEBHOOK_PORT, MODEL_PROFILE, PROMPT_VERSION, AUTH_ENABLED,
    )

    uvicorn.run(app, host=WEBHOOK_HOST, port=WEBHOOK_PORT, log_level="info")


if __name__ == "__main__":
    main()
