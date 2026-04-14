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
    app = create_app(engine)

    log.info("starting search_api host=%s port=%d", SEARCH_HOST, SEARCH_PORT)
    uvicorn.run(app, host=SEARCH_HOST, port=SEARCH_PORT, log_level="info")


if __name__ == "__main__":
    main()
