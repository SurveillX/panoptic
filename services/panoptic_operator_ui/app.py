"""
Panoptic Operator UI — FastAPI app factory.

M10 P10.2 wires only the trailer-day page and a minimal fleet
placeholder. Fleet page, search page, detail pages arrive in later
phases (P10.3, P10.4).

Architecture rule: this module NEVER touches Postgres, Redis, or
stored files directly. All data comes from the Search API via
client.SearchAPIClient.
"""

from __future__ import annotations

import logging
from pathlib import Path

import html
import re

import httpx
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .client import SEARCH_API_URL, AgentClient, SearchAPIClient

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


_CITATION_MARKER_RE = re.compile(r"\[([0-9a-fA-F]{6,70})\]")

_TYPE_TO_URL_PREFIX = {
    "event":   "/events/",
    "image":   "/images/",
    "summary": "/summaries/",
    "report":  "/reports/",  # report URLs look like /reports/<id>/view
}


def _render_citations(text: str, cited_by_id: dict[str, str]) -> Markup:
    """Replace [<full-hex>] markers with clickable anchor tags to the
    right detail page. Unknown or unverified markers render as a
    `.cite-unknown` span so they're visible but don't have a live link.

    Called from templates as `render_citations(text, cited_by_id)`.
    `cited_by_id` is a dict[full_hex_id, type] built from the agent's
    `citations` list. Everything else (including the non-marker prose)
    is HTML-escaped for safety.
    """
    if not text:
        return Markup("")

    out_parts: list[str] = []
    last = 0
    for m in _CITATION_MARKER_RE.finditer(text):
        out_parts.append(html.escape(text[last : m.start()]))
        raw_id = m.group(1).lower()
        ctype = cited_by_id.get(raw_id)
        short = raw_id[:12]
        if ctype and ctype in _TYPE_TO_URL_PREFIX:
            prefix = _TYPE_TO_URL_PREFIX[ctype]
            url = (
                f"{prefix}{raw_id}/view"
                if ctype == "report"
                else f"{prefix}{raw_id}"
            )
            out_parts.append(
                f'<a class="cite cite-{html.escape(ctype)}" '
                f'href="{html.escape(url)}" '
                f'title="{html.escape(ctype)} {html.escape(raw_id)}">'
                f'[{html.escape(short)}…]</a>'
            )
        else:
            out_parts.append(
                f'<span class="cite cite-unknown" '
                f'title="unresolved: {html.escape(raw_id)}">'
                f'[{html.escape(short)}…]</span>'
            )
        last = m.end()
    out_parts.append(html.escape(text[last:]))
    return Markup("".join(out_parts))


def _build_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals["render_citations"] = _render_citations
    return env


def create_app() -> FastAPI:
    app = FastAPI(title="Panoptic Operator UI", version="1.0")
    env = _build_jinja_env()
    client = SearchAPIClient()
    agent_client = AgentClient()

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render(template_name: str, **ctx) -> HTMLResponse:
        tmpl = env.get_template(template_name)
        return HTMLResponse(tmpl.render(**ctx))

    def _render_404(title: str, detail: str) -> HTMLResponse:
        tmpl = env.get_template("404.html.j2")
        return HTMLResponse(tmpl.render(title=title, detail=detail), status_code=404)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        try:
            upstream = client.health()
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "service": "panoptic_operator_ui",
                    "search_api_url": SEARCH_API_URL,
                    "search_api_reachable": False,
                    "error": str(exc)[:200],
                },
            )
        return {
            "status": "ok",
            "service": "panoptic_operator_ui",
            "search_api_url": SEARCH_API_URL,
            "search_api_reachable": True,
            "search_api_status": upstream.get("status"),
        }

    # ------------------------------------------------------------------
    # Fleet index
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def fleet_index():
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            data = client.fleet_overview()
        except httpx.HTTPError as exc:
            log.exception("fleet: upstream failed")
            return HTMLResponse(
                content=env.get_template("404.html.j2").render(
                    title="Search API unreachable",
                    detail=f"Could not reach {SEARCH_API_URL}: {exc!s}",
                ),
                status_code=503,
            )
        return _render("fleet.html.j2", data=data, today=today)

    # ------------------------------------------------------------------
    # Trailer day — the M10 first vertical slice
    # ------------------------------------------------------------------

    def _render_trailer_day(
        serial_number: str,
        day: str,
        *,
        ask_question: str | None = None,
        ask_response: dict | None = None,
        ask_error: str | None = None,
    ) -> HTMLResponse:
        """Shared renderer for the trailer-day page. Handles both the
        plain GET path and the POST-ask path (which re-renders with an
        agent response block populated)."""
        try:
            day_data = client.trailer_day(serial_number, day)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                return _render_404(
                    "Invalid request",
                    f"The date {day!r} could not be parsed. Expected YYYY-MM-DD.",
                )
            log.exception("trailer_day upstream failed")
            return _render_404(
                "Upstream error",
                f"Search API returned {exc.response.status_code} for "
                f"/v1/trailer/{serial_number}/day/{day}.",
            )
        except httpx.HTTPError as exc:
            log.exception("trailer_day upstream network error")
            return HTMLResponse(
                content=env.get_template("404.html.j2").render(
                    title="Search API unreachable",
                    detail=f"Could not reach {SEARCH_API_URL}: {exc!s}",
                ),
                status_code=503,
            )

        # Fetch report history (latest 5 daily + 2 weekly). Degrades.
        report_history: dict[str, list] = {"daily": [], "weekly": []}
        try:
            daily_list = client.reports_list(
                serial_number=serial_number, kind="daily", limit=5,
            )
            report_history["daily"] = daily_list.get("reports", [])
            weekly_list = client.reports_list(
                serial_number=serial_number, kind="weekly", limit=2,
            )
            report_history["weekly"] = weekly_list.get("reports", [])
        except Exception as exc:
            log.warning("trailer_day: report-history fetch failed: %s", exc)

        # Compute prev/next day links.
        from datetime import date, timedelta
        try:
            d = date.fromisoformat(day)
            prev_day = (d - timedelta(days=1)).isoformat()
            next_day = (d + timedelta(days=1)).isoformat()
        except ValueError:
            prev_day = None
            next_day = None

        return _render(
            "trailer_day.html.j2",
            day_data=day_data,
            report_history=report_history,
            search_api_url=SEARCH_API_URL,
            prev_day=prev_day,
            next_day=next_day,
            ask_question=ask_question,
            ask_response=ask_response,
            ask_error=ask_error,
        )

    @app.get("/trailer/{serial_number}/{day}", response_class=HTMLResponse)
    def trailer_day(serial_number: str, day: str):
        return _render_trailer_day(serial_number, day)

    # ------------------------------------------------------------------
    # POST /trailer/{serial}/{day}/ask — call /v1/agent/ask scoped to
    # this trailer-day, re-render the trailer-day page with the response
    # baked in. Single-shot UX; no session state; URL stays stable so
    # browser-back works.
    # ------------------------------------------------------------------

    @app.post("/trailer/{serial_number}/{day}/ask", response_class=HTMLResponse)
    def trailer_day_ask(
        serial_number: str,
        day: str,
        question: str = Form(...),
    ):
        q = (question or "").strip()
        if not q:
            return _render_trailer_day(
                serial_number, day,
                ask_question="",
                ask_error="Please enter a question.",
            )

        try:
            resp = agent_client.ask(
                question=q,
                scope={"serial_number": serial_number, "date": day},
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = exc.response.text[:300] if exc.response is not None else ""
            log.warning("agent_ask upstream %s: %s", status, detail)
            return _render_trailer_day(
                serial_number, day,
                ask_question=q,
                ask_error=f"Agent returned {status}: {detail}",
            )
        except httpx.HTTPError as exc:
            log.exception("agent_ask network error")
            return _render_trailer_day(
                serial_number, day,
                ask_question=q,
                ask_error=f"Could not reach agent service: {exc!s}",
            )

        return _render_trailer_day(
            serial_number, day,
            ask_question=q,
            ask_response=resp,
        )

    # ------------------------------------------------------------------
    # Search page (URL-driven — query string is source of truth)
    # ------------------------------------------------------------------

    _ALLOWED_TYPES = {"event", "image", "summary"}
    _SEMANTIC_TYPES = {"image", "summary"}  # require a query

    @app.get("/search", response_class=HTMLResponse)
    def search(
        q: str | None = None,
        rt: list[str] | None = Query(default=None, alias="type"),
        serial: str | None = None,
        camera: str | None = None,
        top_k: int = 10,
    ):
        # Parse / sanitize params.
        query = (q or "").strip() or None

        # `type` arrives as a repeated query param (?type=event&type=image);
        # also tolerate comma-separated for shareable URLs. Aliased to `rt`
        # in the Python signature because `type` shadows a builtin and
        # FastAPI's default dependency injection misbehaves for it.
        raw_types = list(rt or [])
        types: list[str] = []
        seen: set[str] = set()
        for t in raw_types:
            for piece in str(t).split(","):
                piece = piece.strip()
                if piece in _ALLOWED_TYPES and piece not in seen:
                    seen.add(piece)
                    types.append(piece)
        if not types:
            types = ["event"]  # default — event is the only type with filter-only browse

        # Clamp top_k to the search API's 1..50 range.
        try:
            top_k_val = int(top_k)
        except (TypeError, ValueError):
            top_k_val = 10
        top_k_val = max(1, min(50, top_k_val))

        submitted = bool(query or serial or camera or (rt is not None))

        serial_clean = (serial or "").strip() or None
        camera_clean = (camera or "").strip() or None

        base_ctx = {
            "q": query,
            "types": types,
            "serial": serial_clean,
            "camera": camera_clean,
            "top_k": top_k_val,
            "submitted": submitted,
            "search_api_url": SEARCH_API_URL,
            "error": None,
            "results": {"events": [], "images": [], "summaries": []},
            "total": 0,
            "timing": {"parse": 0, "qdrant": 0, "postgres": 0, "rerank": 0, "total": 0},
        }

        if not submitted:
            return _render("search.html.j2", **base_ctx)

        # Backend requires a query for image/summary types; degrade gracefully
        # when the user ticked only semantic types and left query blank.
        if query is None and any(t in _SEMANTIC_TYPES for t in types):
            # Drop the semantic types rather than 400 — user's URL filter
            # combination isn't supported by the backend.
            effective = [t for t in types if t not in _SEMANTIC_TYPES]
            if not effective:
                base_ctx["error"] = (
                    "This filter combination isn't supported: image and summary "
                    "searches need a query. Add text to the Query field, or "
                    "uncheck images/summaries."
                )
                return _render("search.html.j2", **base_ctx)
            types_to_send = effective
        else:
            types_to_send = list(types)

        try:
            resp = client.search(
                query=query,
                record_types=types_to_send,
                serial_number=serial_clean,
                camera_id=camera_clean,
                top_k=top_k_val,
            )
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300] if exc.response is not None else ""
            base_ctx["error"] = (
                f"Search API returned {exc.response.status_code}: {detail}"
            )
            return _render("search.html.j2", **base_ctx)
        except httpx.HTTPError as exc:
            log.exception("search: upstream network error")
            base_ctx["error"] = f"Could not reach Search API: {exc!s}"
            return _render("search.html.j2", **base_ctx)

        base_ctx["results"] = resp.get("results") or {}
        # Normalize in case any section is missing.
        for k in ("events", "images", "summaries"):
            base_ctx["results"].setdefault(k, [])
        base_ctx["total"] = resp.get("total", 0)
        base_ctx["timing"] = resp.get("timing_ms") or base_ctx["timing"]
        return _render("search.html.j2", **base_ctx)

    # ------------------------------------------------------------------
    # Detail pages (P10.4)
    # ------------------------------------------------------------------

    def _day_from_iso(ts: str | None) -> str | None:
        """Pull the YYYY-MM-DD prefix out of an ISO timestamp so detail
        pages can link back to the correct trailer-day view."""
        if not ts or len(ts) < 10:
            return None
        return ts[:10]

    def _handle_upstream_404(exc: httpx.HTTPStatusError, title: str):
        """Render a friendly 404 page when the Search API returns 404."""
        return _render_404(
            title, f"The requested {title.lower()} does not exist."
        )

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event_detail_page(event_id: str):
        try:
            ev = client.event_detail(event_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return _render_404("Event not found",
                                   f"No event with id <code>{event_id[:16]}…</code>.")
            log.exception("event_detail upstream error")
            return _render_404("Upstream error",
                               f"Search API returned {exc.response.status_code}.")
        except httpx.HTTPError as exc:
            log.exception("event_detail upstream network error")
            return HTMLResponse(
                content=env.get_template("404.html.j2").render(
                    title="Search API unreachable",
                    detail=f"{exc!s}",
                ),
                status_code=503,
            )
        day = _day_from_iso(ev.get("event_time_utc")) or _day_from_iso(ev.get("start_time_utc")) or ""
        return _render("event_detail.html.j2",
                       event=ev, day=day, search_api_url=SEARCH_API_URL)

    @app.get("/summaries/{summary_id}", response_class=HTMLResponse)
    def summary_detail_page(summary_id: str):
        try:
            s = client.summary_detail(summary_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return _render_404("Summary not found",
                                   f"No summary with id <code>{summary_id[:16]}…</code>.")
            log.exception("summary_detail upstream error")
            return _render_404("Upstream error",
                               f"Search API returned {exc.response.status_code}.")
        except httpx.HTTPError as exc:
            log.exception("summary_detail upstream network error")
            return HTMLResponse(
                content=env.get_template("404.html.j2").render(
                    title="Search API unreachable",
                    detail=f"{exc!s}",
                ),
                status_code=503,
            )
        day = _day_from_iso(s.get("start_time")) or ""
        return _render("summary_detail.html.j2",
                       summary=s, day=day, search_api_url=SEARCH_API_URL)

    @app.get("/images/{image_id}", response_class=HTMLResponse)
    def image_detail_page(image_id: str):
        try:
            img = client.image_detail(image_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return _render_404("Image not found",
                                   f"No image with id <code>{image_id[:16]}…</code>.")
            log.exception("image_detail upstream error")
            return _render_404("Upstream error",
                               f"Search API returned {exc.response.status_code}.")
        except httpx.HTTPError as exc:
            log.exception("image_detail upstream network error")
            return HTMLResponse(
                content=env.get_template("404.html.j2").render(
                    title="Search API unreachable",
                    detail=f"{exc!s}",
                ),
                status_code=503,
            )
        day = _day_from_iso(img.get("captured_at_utc")) or _day_from_iso(img.get("bucket_start_utc")) or ""
        return _render("image_detail.html.j2",
                       image=img, day=day, search_api_url=SEARCH_API_URL)

    # ------------------------------------------------------------------
    # POST /trailer/{serial}/{day}/generate-report — enqueue a daily and
    # 303-redirect back to the trailer-day view. Idempotent: if the
    # report already exists the upstream returns {status: success}
    # immediately and we just refresh.
    # ------------------------------------------------------------------

    @app.post("/trailer/{serial_number}/{day}/generate-report")
    def generate_daily_report_from_trailer_day(serial_number: str, day: str):
        try:
            client.enqueue_daily_report(serial_number, day)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "generate-report upstream rejected: %s %s",
                exc.response.status_code, exc.response.text[:200],
            )
        except httpx.HTTPError as exc:
            log.exception("generate-report upstream network error: %s", exc)
        # Always 303 back to the page — the updated status renders on reload.
        return RedirectResponse(
            url=f"/trailer/{serial_number}/{day}",
            status_code=303,
        )

    @app.get("/reports/{report_id}/view", response_class=HTMLResponse)
    def report_viewer_page(report_id: str):
        try:
            report = client.report_status(report_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return _render_404("Report not found",
                                   f"No report with id <code>{report_id[:16]}…</code>.")
            log.exception("report_viewer upstream error")
            return _render_404("Upstream error",
                               f"Search API returned {exc.response.status_code}.")
        except httpx.HTTPError as exc:
            log.exception("report_viewer upstream network error")
            return HTMLResponse(
                content=env.get_template("404.html.j2").render(
                    title="Search API unreachable",
                    detail=f"{exc!s}",
                ),
                status_code=503,
            )
        return _render("report_viewer.html.j2",
                       report=report, search_api_url=SEARCH_API_URL)

    return app
