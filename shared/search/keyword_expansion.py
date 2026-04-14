"""
Keyword expansion for summary search.

Maps free-text signal keywords in a user query to the canonical phrases
injected into summaries by the signal pipeline. Used by both the CLI
(scripts/query_summaries.py) and the Search API to pre-filter Qdrant on
`key_events_labels` and to expand the query text sent to the embedding model.
"""

from __future__ import annotations


SIGNAL_MAP: dict[str, str] = {
    "spike":             "spike in activity",
    "drop":              "drop in activity",
    "after hours":       "after hours activity",
    "start of activity": "start of activity",
    "start":             "start of activity",
    "late start":        "late start",
    "late":              "late start",
    "underperforming":   "underperforming",
    "underperform":      "underperforming",
}


def extract_canonical_labels(text: str) -> list[str]:
    """Return canonical label phrases whose keyword appears in `text`."""
    if not text:
        return []
    query_lower = text.lower()
    labels: list[str] = []
    seen: set[str] = set()
    for keyword, canonical in SIGNAL_MAP.items():
        if keyword in query_lower and canonical not in seen:
            labels.append(canonical)
            seen.add(canonical)
    return labels


def expand_query(text: str) -> str:
    """
    Return text augmented with canonical signal phrases matched in the query.

    If no keywords match, returns the input unchanged.
    """
    labels = extract_canonical_labels(text)
    if not labels:
        return text
    return " ".join(labels)
