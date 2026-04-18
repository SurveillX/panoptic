"""
Search API server — entrypoint.

Usage:
    DATABASE_URL=postgresql://user:pass@localhost/dbname \
    PYTHONPATH=. python -m services.search_api.server

Environment variables:
    DATABASE_URL     — Postgres connection string (required)
    SEARCH_HOST      — bind host (default: 0.0.0.0)
    SEARCH_PORT      — bind port (default: 8600)
"""

from __future__ import annotations

import logging
import os

import uvicorn
from sqlalchemy import create_engine

from services.search_api.app import create_app
from services.search_api.warmup import start_warmup
from shared.health.probes import start_probe_loop
from shared.health.state import HealthState
from shared.utils.leases import generate_worker_id

log = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://localhost/panoptic")
SEARCH_HOST: str = os.environ.get("SEARCH_HOST", "0.0.0.0")
SEARCH_PORT: int = int(os.environ.get("SEARCH_PORT", "8600"))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    health = HealthState(service_name="search_api", worker_id=generate_worker_id())
    health.mark_critical("postgres", "qdrant", "retrieval")
    start_probe_loop(
        health,
        targets={
            "postgres": {"database_url": DATABASE_URL},
            "qdrant": {"qdrant_url": os.environ.get("QDRANT_URL", "http://localhost:6333")},
            "retrieval": {"retrieval_url": os.environ.get("RETRIEVAL_BASE_URL", "http://localhost:8700")},
        },
    )

    app = create_app(engine, health_state=health)

    # Pre-warm the retrieval-service rerank path so the first real /v1/search
    # doesn't pay the ~100s torch.compile cost. Runs in the background.
    start_warmup(delay_sec=2.0)

    log.info("starting search_api host=%s port=%d", SEARCH_HOST, SEARCH_PORT)
    uvicorn.run(app, host=SEARCH_HOST, port=SEARCH_PORT, log_level="info")


if __name__ == "__main__":
    main()
