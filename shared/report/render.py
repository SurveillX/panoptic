"""
HTML rendering for M9 reports.

Jinja2 environment + thin `render_daily` / `render_weekly` entrypoints.
Templates live in shared/report/templates/. Autoescape is ON — every
field reaching a template is user-ingested (captions, summaries) and
could in principle contain HTML.

Images referenced in the rendered HTML use the authorized asset
endpoint `/v1/reports/{report_id}/assets/{image_id}.jpg` — NOT inline
base64 data-URIs. This keeps the HTML small and matches the M10 UI
story.

Template version constant here is surfaced in `metadata_json.template_version`
so consumers can tell which template rendered a given stored HTML file.
Bumping it does NOT invalidate existing reports — rerun is optional.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)


TEMPLATE_VERSION = "v1"

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def render_daily(context: dict[str, Any]) -> str:
    """Render the daily HTML report. Context keys expected:
      report_id, serial_number, window_start_utc, window_end_utc,
      generated_at_utc, per_camera (list of dicts), overall (dict),
      events (list), image_count, summary_count, event_count,
      camera_count, asset_url_prefix, template_version.
    """
    tmpl = _env.get_template("daily.html.j2")
    return tmpl.render(**context)


def render_weekly(context: dict[str, Any]) -> str:
    """Render the weekly HTML report. Context shape is M9 P9.4."""
    tmpl = _env.get_template("weekly.html.j2")
    return tmpl.render(**context)
