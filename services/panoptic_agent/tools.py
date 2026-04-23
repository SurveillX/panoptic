"""
Tool schemas for the Panoptic agent and dispatch to SearchAPIClient.

Tools are 1:1 wrappers over existing Search API endpoints. Nothing new
lands on the backend for M11 v1.

## generate_daily_report gating

Per M11 plan guardrail: `generate_daily_report` is **only exposed to
the model when the user's question is classified as report-related**.
This prevents the model from choosing "generate a report" as a lazy
resolution for any uncertainty. The classifier is a simple regex in
`is_report_related_question`.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .client import SearchAPIClient


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic tool_use format)
# ---------------------------------------------------------------------------

SEARCH_TOOL = {
    "name": "search",
    "description": (
        "Hybrid semantic + filter search over Panoptic events, images, and "
        "summaries. Use for 'find X' or 'show me Y'. Supports exact-match "
        "filters on serial_number, camera_id, time_range, event_type, "
        "event_source, trigger, summary_level. Returns grouped results "
        "with real IDs you can cite."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query. Required for image/summary types; optional for event-only filter-browse.",
            },
            "record_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["summary", "image", "event"]},
                "description": "One or more of: summary, image, event.",
            },
            "filters": {
                "type": "object",
                "properties": {
                    "serial_number": {"type": "string"},
                    "camera_id": {"type": "string"},
                    "time_range": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string", "description": "ISO 8601 UTC"},
                            "end":   {"type": "string", "description": "ISO 8601 UTC"},
                        },
                        "required": ["start", "end"],
                    },
                    "trigger": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["alert", "anomaly", "baseline"]},
                    },
                    "event_type":   {"type": "array", "items": {"type": "string"}},
                    "event_source": {"type": "array", "items": {"type": "string"}},
                    "summary_level":{"type": "array", "items": {"type": "string"}},
                },
            },
            "top_k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
        "required": ["record_types"],
    },
}

VERIFY_TOOL = {
    "name": "verify",
    "description": (
        "Run VLM-grounded verification of a specific claim against recent "
        "evidence. Returns a verdict (supported / partially_supported / "
        "not_supported / insufficient_evidence) with supporting IDs. Use this "
        "when the user asks to verify/confirm a specific claim."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "record_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["summary", "image", "event"]},
            },
            "filters": {"type": "object"},
            "search_top_k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
    },
}

SUMMARIZE_PERIOD_TOOL = {
    "name": "summarize_period",
    "description": (
        "Generate a multi-camera period narrative for a (trailer, time_range) "
        "optionally scoped to a subset of cameras (e.g., 'construction cameras')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "serial_number": {"type": "string"},
            "camera_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset. Omit to include every camera with data.",
            },
            "time_range_start": {"type": "string", "description": "ISO 8601 UTC"},
            "time_range_end":   {"type": "string", "description": "ISO 8601 UTC"},
            "summary_type": {"type": "string", "enum": ["operational", "progress", "mixed"], "default": "operational"},
        },
        "required": ["serial_number", "time_range_start", "time_range_end"],
    },
}

GET_TRAILER_DAY_TOOL = {
    "name": "get_trailer_day",
    "description": (
        "One-call rollup for (trailer, UTC date): events, top images, "
        "summaries, per-camera counts, latest daily report id. Your first "
        "call when the user provides (serial, date) scope."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "serial_number": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD (UTC)"},
        },
        "required": ["serial_number", "date"],
    },
}

GET_FLEET_OVERVIEW_TOOL = {
    "name": "get_fleet_overview",
    "description": (
        "Active trailers with last-seen, 24h event count, latest daily "
        "report. Use for 'which trailer is busiest' or 'is X currently "
        "online'. Capped at 50 trailers."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

GET_EVENT_TOOL = {
    "name": "get_event",
    "description": "Full panoptic_events row for one event_id.",
    "input_schema": {
        "type": "object",
        "properties": {"event_id": {"type": "string"}},
        "required": ["event_id"],
    },
}

GET_SUMMARY_TOOL = {
    "name": "get_summary",
    "description": "Full panoptic_summaries row for one summary_id.",
    "input_schema": {
        "type": "object",
        "properties": {"summary_id": {"type": "string"}},
        "required": ["summary_id"],
    },
}

GET_IMAGE_TOOL = {
    "name": "get_image",
    "description": (
        "Image metadata (NOT the bytes) for one image_id: trigger, camera, "
        "caption, timestamps, dimensions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"image_id": {"type": "string"}},
        "required": ["image_id"],
    },
}

LIST_REPORTS_TOOL = {
    "name": "list_reports",
    "description": (
        "Recent report metadata rows in reverse-chronological order by "
        "window_start_utc. Useful for 'what reports exist for X'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "serial_number": {"type": "string"},
            "kind":          {"type": "string", "enum": ["daily", "weekly"]},
            "limit":         {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
    },
}

GET_REPORT_TOOL = {
    "name": "get_report",
    "description": (
        "Report status + metadata for one report_id. Use to check whether a "
        "report is pending / running / success / failed, and to read its "
        "narratives + cited evidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"report_id": {"type": "string"}},
        "required": ["report_id"],
    },
}

PULL_FRAME_TOOL = {
    "name": "pull_frame",
    "description": (
        "Fetch a JPEG frame from a trailer at a specific moment when the "
        "cached images don't cover the timestamp you need. Useful when "
        "the user asks about a moment that falls between cached "
        "baseline/novelty images, or when you need direct visual "
        "evidence to ground a claim. Pulled frames persist as normal "
        "panoptic_images rows with source='on_demand_pull' — you can "
        "cite the returned image_id like any other image. Caption "
        "enrichment is asynchronous; the image_id is returned "
        "immediately with caption_status='pending'. Call get_image later "
        "if you need the caption text. Use sparingly — each pull costs "
        "a trailer round-trip and caption worker budget."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "serial_number": {"type": "string"},
            "camera_id":     {"type": "string"},
            "timestamp_utc": {
                "type": "string",
                "description": "ISO 8601 UTC (e.g., '2026-04-23T14:35:12+00:00').",
            },
            "reason": {
                "type": "string",
                "description": "Optional audit note — why this frame was pulled.",
            },
        },
        "required": ["serial_number", "camera_id", "timestamp_utc"],
    },
}


GENERATE_DAILY_REPORT_TOOL = {
    "name": "generate_daily_report",
    "description": (
        "Enqueue a daily report for (serial_number, date). Returns "
        "{report_id, status}. IDEMPOTENT: calling twice for the same window "
        "returns the same report_id. ONLY call this when the user explicitly "
        "asks to generate/create/produce a report. Do NOT call to resolve "
        "uncertainty — uncertainty belongs in the answer's hedged wording."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "serial_number": {"type": "string"},
            "date":          {"type": "string", "description": "YYYY-MM-DD (UTC)"},
        },
        "required": ["serial_number", "date"],
    },
}


# Tools always available to the model (12 total including M14 pull_frame;
# generate_daily_report is gated separately behind a report-intent regex).
_ALWAYS_TOOLS = [
    SEARCH_TOOL,
    VERIFY_TOOL,
    SUMMARIZE_PERIOD_TOOL,
    GET_TRAILER_DAY_TOOL,
    GET_FLEET_OVERVIEW_TOOL,
    GET_EVENT_TOOL,
    GET_SUMMARY_TOOL,
    GET_IMAGE_TOOL,
    LIST_REPORTS_TOOL,
    GET_REPORT_TOOL,
    PULL_FRAME_TOOL,
]

_REPORT_WRITE_TOOL = GENERATE_DAILY_REPORT_TOOL


# ---------------------------------------------------------------------------
# Report-intent classifier
# ---------------------------------------------------------------------------

_REPORT_INTENT_RE = re.compile(
    r"\b("
    r"generate\s+(a|the|today's|yesterday's)?\s*(daily\s+)?report"
    r"|make\s+(a|the)?\s*(daily\s+)?report"
    r"|create\s+(a|the)?\s*(daily\s+)?report"
    r"|build\s+(a|the)?\s*(daily\s+)?report"
    r"|produce\s+(a|the)?\s*(daily\s+)?report"
    r"|run\s+(the\s+)?daily\s+report"
    r"|daily\s+report\s+for"
    r")\b",
    re.IGNORECASE,
)


def is_report_related_question(question: str) -> bool:
    """
    Returns True iff the question mentions generating/creating/producing
    a report in a way that warrants exposing the write tool. False
    positives are acceptable — the worst case is the model sees the
    tool but doesn't need it. False negatives are the dangerous case:
    the user asked for a report, tool isn't in scope, agent has to
    explain it can't.
    """
    if not question:
        return False
    return bool(_REPORT_INTENT_RE.search(question))


def tools_for_question(question: str) -> list[dict]:
    """Return the tool schema list visible to the model for this ask."""
    tools = list(_ALWAYS_TOOLS)
    if is_report_related_question(question):
        tools.append(_REPORT_WRITE_TOOL)
    return tools


# ---------------------------------------------------------------------------
# Dispatch — maps tool_name → SearchAPIClient method
# ---------------------------------------------------------------------------


def dispatch_tool(
    client: SearchAPIClient,
    *,
    tool_name: str,
    tool_input: dict,
    allow_write: bool,
) -> Any:
    """
    Call the Search API method the model requested. Returns the decoded
    JSON. Raises on unknown tool name, or if allow_write=False and the
    model tries to call the write tool.
    """
    if tool_name == "search":
        return client.search(
            query=tool_input.get("query"),
            record_types=tool_input.get("record_types", ["event"]),
            filters=tool_input.get("filters") or None,
            top_k=int(tool_input.get("top_k", 10)),
        )
    if tool_name == "verify":
        return client.verify(
            query=tool_input["query"],
            record_types=tool_input.get("record_types"),
            filters=tool_input.get("filters") or None,
            search_top_k=int(tool_input.get("search_top_k", 10)),
        )
    if tool_name == "summarize_period":
        return client.summarize_period(
            serial_number=tool_input["serial_number"],
            camera_ids=tool_input.get("camera_ids"),
            time_range_start=tool_input["time_range_start"],
            time_range_end=tool_input["time_range_end"],
            summary_type=tool_input.get("summary_type", "operational"),
        )
    if tool_name == "get_trailer_day":
        return client.get_trailer_day(
            serial_number=tool_input["serial_number"],
            date=tool_input["date"],
        )
    if tool_name == "get_fleet_overview":
        return client.get_fleet_overview()
    if tool_name == "get_event":
        return client.get_event(event_id=tool_input["event_id"])
    if tool_name == "get_summary":
        return client.get_summary(summary_id=tool_input["summary_id"])
    if tool_name == "get_image":
        return client.get_image(image_id=tool_input["image_id"])
    if tool_name == "list_reports":
        return client.list_reports(
            serial_number=tool_input.get("serial_number"),
            kind=tool_input.get("kind"),
            limit=int(tool_input.get("limit", 10)),
        )
    if tool_name == "get_report":
        return client.get_report(report_id=tool_input["report_id"])

    if tool_name == "pull_frame":
        return client.pull_frame(
            serial_number=tool_input["serial_number"],
            camera_id=tool_input["camera_id"],
            timestamp_utc=tool_input["timestamp_utc"],
            reason=tool_input.get("reason"),
        )

    if tool_name == "generate_daily_report":
        if not allow_write:
            raise PermissionError(
                "generate_daily_report was not included in this request's "
                "tool list (question not classified as report-related)."
            )
        return client.generate_daily_report(
            serial_number=tool_input["serial_number"],
            date=tool_input["date"],
        )

    raise ValueError(f"unknown tool: {tool_name!r}")
