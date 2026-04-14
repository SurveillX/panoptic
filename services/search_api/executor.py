"""
Search execution — maps the 6-step pipeline to code.

    1. parse query + filters        (done by Pydantic SearchRequest)
    2. structured narrowing         (only for filter-only event browse)
    3. semantic retrieval (Qdrant)
    4. hydrate records (Postgres)   (image/event only; summaries read from Qdrant payload)
    5. merge/rank                   (v1 rerank stub, per-type)
    6. return grouped results

Time-range filtering is applied in Python after Qdrant returns, because the
payload stores times as ISO strings — post-filtering keeps v1 simple and
correct without requiring a Qdrant-side datetime-range feature.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import text as sa_text

from shared.clients.embedding import EmbeddingClient
from shared.clients.qdrant import search_image_captions, search_summaries
from shared.search.keyword_expansion import expand_query

from .qdrant_filters import build_event_filter, build_image_filter, build_summary_filter
from .rerank import rerank
from .schemas import (
    EventHit,
    ImageHit,
    SearchRequest,
    SearchResponse,
    SearchResults,
    SummaryHit,
    TimingMs,
)

log = logging.getLogger(__name__)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _in_time_range(value: str | None, start: datetime, end: datetime) -> bool:
    dt = _parse_iso(value)
    if dt is None:
        return False
    return start <= dt < end


def _qdrant_limit(top_k: int, has_time_filter: bool) -> int:
    return top_k * 3 if has_time_filter else top_k


def execute_search(req: SearchRequest, engine, embedder: EmbeddingClient) -> SearchResponse:
    t0 = time.perf_counter()

    timing = TimingMs()
    results = SearchResults()

    t_parse_end = time.perf_counter()
    timing.parse = int((t_parse_end - t0) * 1000)

    time_range = req.filters.time_range
    has_time_filter = time_range is not None

    # --- Embed query once if present -----------------------------------------
    vector: list[float] | None = None
    if req.query:
        expanded = expand_query(req.query)
        vector = embedder.embed(expanded)

    # --- Per-type retrieval --------------------------------------------------
    qdrant_ms = 0
    postgres_ms = 0

    # Summary
    if "summary" in req.record_types and vector is not None:
        t = time.perf_counter()
        s_filter = build_summary_filter(req)
        raw = search_summaries(vector, s_filter, _qdrant_limit(req.top_k, has_time_filter))
        qdrant_ms += int((time.perf_counter() - t) * 1000)
        raw = _apply_summary_time_filter(raw, time_range)
        raw = raw[: req.top_k]
        raw = rerank(req.query, raw)
        results.summaries = [_summary_hit_from_qdrant(r) for r in raw]

    # Image
    if "image" in req.record_types and vector is not None:
        t = time.perf_counter()
        i_filter = build_image_filter(req)
        raw = search_image_captions(vector, i_filter, _qdrant_limit(req.top_k, has_time_filter))
        qdrant_ms += int((time.perf_counter() - t) * 1000)
        raw = _apply_image_time_filter(raw, time_range)
        raw = raw[: req.top_k]
        raw = rerank(req.query, raw)
        t = time.perf_counter()
        hydrated = _hydrate_images(engine, [p["payload"]["image_id"] for p in raw if p.get("payload")])
        postgres_ms += int((time.perf_counter() - t) * 1000)
        results.images = [_image_hit(r, hydrated) for r in raw]

    # Event
    if "event" in req.record_types:
        if vector is not None:
            t = time.perf_counter()
            e_filter = build_event_filter(req)
            raw = search_image_captions(vector, e_filter, _qdrant_limit(req.top_k, has_time_filter))
            qdrant_ms += int((time.perf_counter() - t) * 1000)
            raw = _apply_image_time_filter(raw, time_range)
            raw = raw[: req.top_k]
            raw = rerank(req.query, raw)
            t = time.perf_counter()
            hydrated = _hydrate_images(engine, [p["payload"]["image_id"] for p in raw if p.get("payload")])
            postgres_ms += int((time.perf_counter() - t) * 1000)
            results.events = [_event_hit_from_qdrant(r, hydrated) for r in raw]
        else:
            # Filter-only event browse: Postgres only.
            t = time.perf_counter()
            rows = _browse_events(engine, req)
            postgres_ms += int((time.perf_counter() - t) * 1000)
            results.events = [_event_hit_from_row(row) for row in rows]

    timing.qdrant = qdrant_ms
    timing.postgres = postgres_ms
    # rerank is a no-op in v1; keep the field so clients don't see it appear later
    timing.rerank = 0
    timing.total = int((time.perf_counter() - t0) * 1000)

    total = len(results.summaries) + len(results.images) + len(results.events)

    return SearchResponse(
        query=req.query,
        total=total,
        results=results,
        timing_ms=timing,
    )


# ---------------------------------------------------------------------------
# Post-retrieval time filtering
# ---------------------------------------------------------------------------

def _apply_summary_time_filter(raw: list[dict], time_range) -> list[dict]:
    if time_range is None:
        return raw
    kept = []
    for r in raw:
        start_iso = (r.get("payload") or {}).get("start_time")
        if _in_time_range(start_iso, time_range.start, time_range.end):
            kept.append(r)
    return kept


def _apply_image_time_filter(raw: list[dict], time_range) -> list[dict]:
    if time_range is None:
        return raw
    kept = []
    for r in raw:
        payload = r.get("payload") or {}
        # Prefer captured_at when present; fall back to bucket_start.
        ref = payload.get("captured_at") or payload.get("bucket_start")
        if _in_time_range(ref, time_range.start, time_range.end):
            kept.append(r)
    return kept


# ---------------------------------------------------------------------------
# Postgres hydration / structured browse
# ---------------------------------------------------------------------------

def _hydrate_images(engine, image_ids: list[str]) -> dict[str, dict]:
    if not image_ids:
        return {}
    sql = sa_text("""
        SELECT image_id, serial_number, camera_id, scope_id, trigger,
               captured_at_utc, bucket_start_utc, caption_text, storage_path
        FROM panoptic_images
        WHERE image_id = ANY(:ids)
    """)
    out: dict[str, dict] = {}
    with engine.connect() as conn:
        for row in conn.execute(sql, {"ids": image_ids}).mappings():
            out[row["image_id"]] = dict(row)
    return out


def _browse_events(engine, req: SearchRequest) -> list[dict]:
    filters = req.filters
    clauses = ["trigger IN ('alert','anomaly')"]
    params: dict = {"limit": req.top_k}

    if filters.trigger:
        effective = [t for t in filters.trigger if t in ("alert", "anomaly")]
        if not effective:
            return []
        clauses = ["trigger = ANY(:triggers)"]
        params["triggers"] = effective

    if filters.serial_number:
        clauses.append("serial_number = :sn")
        params["sn"] = filters.serial_number
    if filters.camera_id:
        clauses.append("camera_id = :cam")
        params["cam"] = filters.camera_id
    if filters.time_range:
        clauses.append("bucket_start_utc >= :tstart AND bucket_start_utc < :tend")
        params["tstart"] = filters.time_range.start
        params["tend"] = filters.time_range.end

    where = " AND ".join(clauses)
    sql = sa_text(f"""
        SELECT image_id, serial_number, camera_id, scope_id, trigger,
               captured_at_utc, bucket_start_utc, caption_text, storage_path
        FROM panoptic_images
        WHERE {where}
        ORDER BY bucket_start_utc DESC
        LIMIT :limit
    """)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).mappings()]


# ---------------------------------------------------------------------------
# Hit builders
# ---------------------------------------------------------------------------

def _summary_hit_from_qdrant(r: dict) -> SummaryHit:
    payload = r.get("payload") or {}
    return SummaryHit(
        id=str(r.get("id")),
        score=float(r.get("score") or 0.0),
        level=payload.get("level"),
        serial_number=payload.get("serial_number"),
        scope_id=payload.get("scope_id"),
        start_time=payload.get("start_time"),
        end_time=payload.get("end_time"),
        summary=payload.get("summary"),
        key_events_labels=payload.get("key_events_labels") or [],
        confidence=payload.get("confidence"),
    )


def _image_hit(r: dict, hydrated: dict[str, dict]) -> ImageHit:
    payload = r.get("payload") or {}
    image_id = payload.get("image_id")
    row = hydrated.get(image_id, {})
    return ImageHit(
        id=image_id or str(r.get("id")),
        score=float(r.get("score") or 0.0),
        serial_number=payload.get("serial_number") or row.get("serial_number"),
        camera_id=payload.get("camera_id") or row.get("camera_id"),
        scope_id=payload.get("scope_id") or row.get("scope_id"),
        trigger=payload.get("trigger") or row.get("trigger"),
        captured_at=payload.get("captured_at") or _iso(row.get("captured_at_utc")),
        bucket_start=payload.get("bucket_start") or _iso(row.get("bucket_start_utc")),
        caption_text=payload.get("caption_text") or row.get("caption_text"),
        storage_path=row.get("storage_path"),
    )


def _event_hit_from_qdrant(r: dict, hydrated: dict[str, dict]) -> EventHit:
    payload = r.get("payload") or {}
    image_id = payload.get("image_id")
    row = hydrated.get(image_id, {})
    return EventHit(
        id=image_id or str(r.get("id")),
        score=float(r.get("score") or 0.0),
        trigger=payload.get("trigger") or row.get("trigger"),
        serial_number=payload.get("serial_number") or row.get("serial_number"),
        camera_id=payload.get("camera_id") or row.get("camera_id"),
        scope_id=payload.get("scope_id") or row.get("scope_id"),
        captured_at=payload.get("captured_at") or _iso(row.get("captured_at_utc")),
        bucket_start=payload.get("bucket_start") or _iso(row.get("bucket_start_utc")),
        caption_text=payload.get("caption_text") or row.get("caption_text"),
    )


def _event_hit_from_row(row: dict) -> EventHit:
    return EventHit(
        id=row["image_id"],
        score=0.0,
        trigger=row.get("trigger"),
        serial_number=row.get("serial_number"),
        camera_id=row.get("camera_id"),
        scope_id=row.get("scope_id"),
        captured_at=_iso(row.get("captured_at_utc")),
        bucket_start=_iso(row.get("bucket_start_utc")),
        caption_text=row.get("caption_text"),
    )


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)
