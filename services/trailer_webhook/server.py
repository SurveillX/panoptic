"""
Trailer webhook server — entrypoint.

Usage:
    DATABASE_URL=postgresql://user:pass@localhost/dbname \
    PYTHONPATH=. python -m services.trailer_webhook.server

Environment variables:
    DATABASE_URL        — Postgres connection string (required)
    WEBHOOK_HOST        — bind host (default: 0.0.0.0)
    WEBHOOK_PORT        — bind port (default: 8080)
    MODEL_PROFILE       — model profile for ingested jobs (default: default)
    PROMPT_VERSION      — prompt version for ingested jobs (default: v1)
"""

from __future__ import annotations

import logging
import os

import uvicorn
from sqlalchemy import create_engine

from services.trailer_webhook.app import create_app
from shared.utils.redis_client import get_redis_client
from shared.utils.streams import bootstrap_streams

log = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")
WEBHOOK_HOST: str = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = int(os.environ.get("WEBHOOK_PORT", "8080"))
MODEL_PROFILE: str = os.environ.get("MODEL_PROFILE", "default")
PROMPT_VERSION: str = os.environ.get("PROMPT_VERSION", "v1")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    r = get_redis_client()
    bootstrap_streams(r)

    app = create_app(engine, r, model_profile=MODEL_PROFILE, prompt_version=PROMPT_VERSION)

    log.info(
        "starting webhook server host=%s port=%d model=%s prompt=%s",
        WEBHOOK_HOST, WEBHOOK_PORT, MODEL_PROFILE, PROMPT_VERSION,
    )

    uvicorn.run(app, host=WEBHOOK_HOST, port=WEBHOOK_PORT, log_level="info")


if __name__ == "__main__":
    main()
