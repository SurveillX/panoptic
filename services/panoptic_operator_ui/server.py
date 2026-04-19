"""
panoptic-operator-ui — M10 HTTP server entrypoint.

Runs the FastAPI app under uvicorn on OPERATOR_UI_PORT (default 8400).
No DB connection, no Redis — all data reads go through the Search API
via services.panoptic_operator_ui.client.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from services.panoptic_operator_ui.app import create_app

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    port = int(os.environ.get("OPERATOR_UI_PORT", "8400"))
    log.info("panoptic_operator_ui starting on :%d", port)

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
