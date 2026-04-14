"""
Verification prompt — system message and user-prompt builder.

The VLM sees synthetic labels (sum_0, img_0, evt_0, ...) and is required to
echo them back in the response. Server-side code then translates labels
back to real record IDs before returning the API response.
"""

from __future__ import annotations

from .schemas import EventHit, ImageHit, SummaryHit


SYSTEM_MESSAGE = """\
You are a verification model for a surveillance intelligence platform.

Your job: decide whether the provided evidence supports a user's query about \
events observed by trailer-mounted cameras.

Allowed verdicts (choose exactly one):
- "supported": direct evidence in the provided material clearly answers the query in the affirmative.
- "partially_supported": evidence is related or indicative but not conclusive on its own.
- "not_supported": evidence is present and contradicts the query, or is clearly irrelevant to it.
- "insufficient_evidence": too little signal to judge either way.

Calibration rules:
- Prefer the lower verdict when evidence is weak. Do not overclaim.
- An image showing something adjacent to the query (e.g. a truck when asked about a person) is at most "partially_supported".
- If a summary mentions a concept but no image corroborates it, lean "partially_supported".

Evidence ID contract:
- The user message contains items labeled [sum_N], [img_N], [evt_N].
- In your response, cite only labels that appear in the evidence.
- Do not invent IDs. Do not reference labels that were not shown to you.
- Use each label at most once per field.

Output contract:
Return a single JSON object with exactly these keys and nothing else:
  {
    "verdict": "<one of the four allowed verdicts>",
    "confidence": <float 0.0 to 1.0>,
    "supporting_summary_ids": ["sum_N", ...],
    "supporting_image_ids":   ["img_N", ...],
    "supporting_event_ids":   ["evt_N", ...],
    "reason": "<short explanation, max 280 characters>",
    "uncertainties": ["<short note>", ...]
  }

- No markdown. No prose outside the JSON object. No trailing text.
- "reason" must be <= 280 characters.
- "uncertainties" is a list of short strings (<= 5 items). Empty list if none.
- If there is no supporting evidence for a given type, return an empty list for that field.
"""


def build_user_prompt(
    query: str,
    summary_items: list[tuple[str, SummaryHit]],
    image_items: list[tuple[str, ImageHit]],
    event_items: list[tuple[str, EventHit]],
) -> str:
    """
    Compose the user-side text prompt. Image JPEGs are attached separately via
    VLMClient.call(frame_uris=...). The order of labels here must match the
    order of JPEGs passed to the VLM.
    """
    lines: list[str] = []
    lines.append(f"Query: {query}")
    lines.append("")

    if summary_items:
        lines.append("Summaries:")
        for label, s in summary_items:
            time_str = f"{s.start_time} to {s.end_time}" if s.start_time else "unknown time"
            scope = s.scope_id or s.serial_number or "unknown scope"
            labels = ", ".join(s.key_events_labels) if s.key_events_labels else "none"
            body = (s.summary or "").strip().replace("\n", " ")
            lines.append(
                f"[{label}] level={s.level or '?'} time={time_str} scope={scope} "
                f"signals={labels} confidence={s.confidence if s.confidence is not None else '?'}\n"
                f"  summary: {body}"
            )
        lines.append("")

    if event_items:
        lines.append("Events (structured context, some may correspond to images below):")
        for label, e in event_items:
            time_str = e.captured_at or e.bucket_start or "unknown time"
            scope = e.scope_id or f"{e.serial_number or '?'}:{e.camera_id or '?'}"
            caption = (e.caption_text or "").strip().replace("\n", " ")
            lines.append(
                f"[{label}] trigger={e.trigger or '?'} time={time_str} scope={scope}\n"
                f"  caption: {caption or '(no caption available)'}"
            )
        lines.append("")

    if image_items:
        lines.append(
            "Images (each image is attached below; refer to them by their [img_N] label):"
        )
        for label, i in image_items:
            time_str = i.captured_at or i.bucket_start or "unknown time"
            scope = i.scope_id or f"{i.serial_number or '?'}:{i.camera_id or '?'}"
            caption = (i.caption_text or "").strip().replace("\n", " ")
            lines.append(
                f"[{label}] trigger={i.trigger or '?'} time={time_str} scope={scope}\n"
                f"  caption: {caption or '(no caption available)'}"
            )
        lines.append("")

    if not (summary_items or event_items or image_items):
        lines.append("(No evidence available.)")

    lines.append(
        "Evaluate the query against the evidence above and return the JSON object "
        "described in the system message."
    )
    return "\n".join(lines)
