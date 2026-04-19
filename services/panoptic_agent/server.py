"""
panoptic-agent — M11 HTTP server entrypoint.

Runs the FastAPI app under uvicorn on AGENT_PORT (default 8500). The
agent calls the local vLLM service (default http://localhost:8000) for
inference; no external network egress required.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from services.panoptic_agent.app import create_app

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    port = int(os.environ.get("AGENT_PORT", "8500"))
    log.info(
        "panoptic_agent starting on :%d backend=%s model=%s vllm=%s",
        port,
        os.environ.get("AGENT_BACKEND", "vllm"),
        os.environ.get("AGENT_MODEL", "gemma-4-26b-it"),
        os.environ.get("AGENT_VLLM_BASE_URL", "http://localhost:8000"),
    )

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
