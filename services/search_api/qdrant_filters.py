"""
Translate structured search filters into Qdrant payload filter dicts.

Filter semantics (CONTRACT): all identity matches are EXACT equality.
No substring, no prefix, no regex, no ILIKE. If you need fuzzy matching,
that belongs in the embedding/text stage, not in the filter stage.

Time-range filtering is NOT done here — the summary and image Qdrant payloads
store time as ISO strings, which we post-filter in Python after hydration.
"""

from __future__ import annotations

from shared.search.keyword_expansion import extract_canonical_labels

from .schemas import SearchFilters, SearchRequest


def build_summary_filter(req: SearchRequest) -> dict | None:
    """
    Build a Qdrant payload filter for the panoptic_summaries collection.

    - serial_number: exact match on payload.serial_number
    - camera_id: only applies when serial_number also present — exact match
                 on payload.scope_id == "{serial_number}:{camera_id}"
    - summary_level: exact match on payload.level (any-of)
    - key_events_labels: derived from query keywords via signal map
    - trigger / time_range: NOT applied to summaries (trigger is image-only;
      time_range is handled post-retrieval)
    """
    filters = req.filters
    must: list[dict] = []

    if filters.serial_number:
        must.append({"key": "serial_number", "match": {"value": filters.serial_number}})
        if filters.camera_id:
            scope_id = f"{filters.serial_number}:{filters.camera_id}"
            must.append({"key": "scope_id", "match": {"value": scope_id}})

    if filters.summary_level:
        must.append({"key": "level", "match": {"any": list(filters.summary_level)}})

    if req.query:
        labels = extract_canonical_labels(req.query)
        if labels:
            must.append({"key": "key_events_labels", "match": {"any": labels}})

    return {"must": must} if must else None


def _build_image_common_filter(filters: SearchFilters) -> list[dict]:
    must: list[dict] = []
    if filters.serial_number:
        must.append({"key": "serial_number", "match": {"value": filters.serial_number}})
    if filters.camera_id:
        must.append({"key": "camera_id", "match": {"value": filters.camera_id}})
    return must


def build_image_filter(req: SearchRequest) -> dict | None:
    """
    Build a Qdrant payload filter for image results in the image_caption_vectors
    collection.

    - serial_number: exact match
    - camera_id: exact match
    - trigger: any-of the provided values
    - time_range: NOT applied here (ISO-string payload; post-filter)
    """
    must = _build_image_common_filter(req.filters)
    if req.filters.trigger:
        must.append({"key": "trigger", "match": {"any": list(req.filters.trigger)}})
    return {"must": must} if must else None


def build_event_filter(req: SearchRequest) -> dict:
    """
    Build a Qdrant payload filter for event results. Always forces
    trigger IN ('alert','anomaly') — event is a view over alert/anomaly images.

    Any caller-supplied trigger filter is intersected with this allowed set.
    """
    allowed = {"alert", "anomaly"}
    if req.filters.trigger:
        effective = [t for t in req.filters.trigger if t in allowed]
        if not effective:
            # Caller asked for triggers that aren't events (e.g. baseline).
            # Use an impossible match so the search returns empty cleanly.
            effective = ["__none__"]
    else:
        effective = sorted(allowed)

    must = _build_image_common_filter(req.filters)
    must.append({"key": "trigger", "match": {"any": effective}})
    return {"must": must}
