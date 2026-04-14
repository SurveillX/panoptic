"""
Prompts for the period summarization endpoint.

Two VLM calls:
  - per-camera synthesis (multimodal; JPEGs attached)
  - overall fusion (text-only, operates on per-camera JSON outputs)

The VLM sees synthetic labels (sum_N / img_N / evt_N for per-camera;
camera_id strings verbatim for fusion). Server-side code translates
labels back to real record IDs before the response leaves the service.
"""

from __future__ import annotations

from .schemas import CameraSummary, SummaryType


# ---------------------------------------------------------------------------
# Summary-type guidance — folded into both system messages
# ---------------------------------------------------------------------------

_TYPE_GUIDANCE: dict[str, str] = {
    "operational":
        "Emphasize operational signals: activity level, incidents, triggers, "
        "after-hours activity, spikes, drops, alerts. Stay factual about what "
        "the evidence shows.",
    "progress":
        "Emphasize visible change over the period: milestones, build progress, "
        "site state differences. Do NOT infer progress that is not directly "
        "shown. If evidence is sparse, say so.",
    "mixed":
        "Cover both operational signals (activity, incidents) and visible "
        "progress/change. Give balanced weight when both are present.",
}


def _type_guidance_block(summary_type: SummaryType) -> str:
    return _TYPE_GUIDANCE.get(summary_type, _TYPE_GUIDANCE["operational"])


# ---------------------------------------------------------------------------
# Per-camera synthesis
# ---------------------------------------------------------------------------

PER_CAMERA_SYSTEM_MESSAGE = """\
You are a surveillance-intelligence summarizer for a single camera over a \
bounded time period on a work-site trailer.

Given evidence (summaries, images with captions, and alert/anomaly events), \
produce one short summary for that camera.

Grounding rules:
- Stay strictly grounded in the provided evidence. Do not invent activity, \
people, vehicles, progress, or incidents.
- If evidence is sparse, contradictory, or low-confidence, SAY SO in the \
summary and lower the confidence field.
- Prefer understatement to overclaiming.

Evidence ID contract:
- You will see items labeled [sum_N], [img_N], [evt_N].
- In your response, cite only labels that appear in the evidence.
- Do not invent IDs. Use each label at most once per field.

Output contract:
Return a single JSON object with exactly these keys and nothing else:
  {
    "headline": "<one short sentence>",
    "summary":  "<2-4 sentences>",
    "supporting_summary_ids": ["sum_N", ...],
    "supporting_image_ids":   ["img_N", ...],
    "supporting_event_ids":   ["evt_N", ...],
    "confidence": <float 0.0 to 1.0>
  }

- No markdown. No prose outside the JSON object.
- "headline" must be <= 140 characters.
- "summary" must be between 2 and 4 sentences, <= 600 characters.
- confidence reflects how well the evidence supports the stated summary.
"""


FUSION_SYSTEM_MESSAGE = """\
You are fusing per-camera summaries from a work-site trailer into one overall \
period summary.

Grounding rules:
- Work ONLY from the per-camera summaries provided. Do not invent facts beyond \
them. Do not claim activity on a camera that was not provided.
- If the per-camera summaries disagree, acknowledge the tension explicitly.
- If coverage is thin (few cameras, low confidence), say so and lower confidence.

Camera ID contract:
- Each per-camera summary arrives with its camera_id (e.g. "cam-01").
- In supporting_camera_ids, cite only camera_ids that were actually provided.
- Do not invent camera IDs.

Output contract:
Return a single JSON object with exactly these keys and nothing else:
  {
    "headline": "<one short sentence>",
    "summary":  "<3-6 sentences>",
    "supporting_camera_ids": ["cam-01", ...],
    "confidence": <float 0.0 to 1.0>
  }

- No markdown. No prose outside the JSON object.
- "headline" must be <= 160 characters.
- "summary" must be between 3 and 6 sentences, <= 900 characters.
"""


# ---------------------------------------------------------------------------
# User-prompt builders
# ---------------------------------------------------------------------------

def build_per_camera_user_prompt(
    *,
    serial_number: str,
    camera_id: str,
    time_range_start: str,
    time_range_end: str,
    summary_type: SummaryType,
    summary_items: list[tuple[str, dict]],
    image_items: list[tuple[str, dict]],
    event_items: list[tuple[str, dict]],
) -> str:
    """
    Build the user message for one per-camera synthesis call.

    `summary_items`, `image_items`, `event_items` are ordered lists of
    (synthetic_label, record_dict) pairs. Image JPEGs are attached via the
    VLM client's frame_uris, in the same order as image_items.
    """
    lines: list[str] = []
    lines.append(f"Trailer: {serial_number}")
    lines.append(f"Camera: {camera_id}")
    lines.append(f"Time range: {time_range_start} to {time_range_end}")
    lines.append(f"Summary type: {summary_type}")
    lines.append(f"Focus: {_type_guidance_block(summary_type)}")
    lines.append("")

    if summary_items:
        lines.append("Summaries:")
        for label, s in summary_items:
            lines.append(_render_summary_item(label, s))
        lines.append("")

    if event_items:
        lines.append("Events (alert/anomaly; structured context, may correspond to images below):")
        for label, e in event_items:
            lines.append(_render_event_item(label, e))
        lines.append("")

    if image_items:
        lines.append("Images (each is attached below; refer by [img_N] label):")
        for label, i in image_items:
            lines.append(_render_image_item(label, i))
        lines.append("")

    if not (summary_items or event_items or image_items):
        lines.append("(No evidence available for this camera in this period.)")

    lines.append(
        "Produce one JSON object matching the schema in the system message. "
        "Ground every claim in the evidence above."
    )
    return "\n".join(lines)


def build_fusion_user_prompt(
    *,
    serial_number: str,
    time_range_start: str,
    time_range_end: str,
    summary_type: SummaryType,
    camera_summaries: list[CameraSummary],
) -> str:
    lines: list[str] = []
    lines.append(f"Trailer: {serial_number}")
    lines.append(f"Time range: {time_range_start} to {time_range_end}")
    lines.append(f"Summary type: {summary_type}")
    lines.append(f"Focus: {_type_guidance_block(summary_type)}")
    lines.append("")
    lines.append("Per-camera summaries:")

    for cs in camera_summaries:
        lines.append(
            f"Camera {cs.camera_id} (confidence={cs.confidence:.2f})\n"
            f"  headline: {cs.headline}\n"
            f"  summary: {cs.summary}"
        )
    lines.append("")
    lines.append(
        "Produce one JSON object matching the schema in the system message. "
        "Stay grounded in the per-camera summaries above."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal rendering helpers
# ---------------------------------------------------------------------------

def _render_summary_item(label: str, s: dict) -> str:
    start = s.get("start_time") or "?"
    end = s.get("end_time") or "?"
    lvl = s.get("level") or "?"
    labels = s.get("key_events_labels") or []
    labels_str = ", ".join(labels) if labels else "none"
    conf = s.get("confidence")
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
    body = (s.get("summary") or "").strip().replace("\n", " ")
    return (
        f"[{label}] level={lvl} time={start} to {end} signals={labels_str} "
        f"confidence={conf_str}\n  summary: {body}"
    )


def _render_event_item(label: str, e: dict) -> str:
    trg = e.get("trigger") or "?"
    when = e.get("captured_at") or e.get("bucket_start") or "?"
    cap = (e.get("caption_text") or "").strip().replace("\n", " ")
    return (
        f"[{label}] trigger={trg} time={when}\n"
        f"  caption: {cap or '(no caption available)'}"
    )


def _render_image_item(label: str, i: dict) -> str:
    trg = i.get("trigger") or "?"
    when = i.get("captured_at") or i.get("bucket_start") or "?"
    cap = (i.get("caption_text") or "").strip().replace("\n", " ")
    return (
        f"[{label}] trigger={trg} time={when}\n"
        f"  caption: {cap or '(no caption available)'}"
    )
