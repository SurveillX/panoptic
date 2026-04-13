import sys
import argparse
import httpx

from shared.clients.embedding import EmbeddingClient

QDRANT_URL = "http://localhost:6333"
COLLECTION = "vil_summaries"

SIGNAL_MAP = {
    "spike":            "spike in activity",
    "drop":             "drop in activity",
    "after hours":      "after hours activity",
    "start of activity": "start of activity",
    "start":            "start of activity",
    "late start":       "late start",
    "late":             "late start",
    "underperforming":  "underperforming",
    "underperform":     "underperforming",
}


def _expand_query(text: str) -> str:
    """Map signal keywords in the query to canonical phrases."""
    query_lower = text.lower()
    phrases = []
    for keyword, canonical in SIGNAL_MAP.items():
        if keyword in query_lower:
            phrases.append(canonical)
    if phrases:
        return " ".join(phrases)
    return text


def _build_filter(
    text: str,
    serial_number: str | None = None,
    camera_id: str | None = None,
) -> dict | None:
    """Build a Qdrant filter matching normalized labels in key_events_labels.

    Optional identity filters:
      --serial-number only:  filter on serial_number field
      --camera-id only:      convenience filter on scope_id containing that
                             camera_id — non-unique, may match across trailers
      both provided:         exact composite match on scope_id == "{sn}:{cam}"
    """
    query_lower = text.lower()
    conditions = []
    for keyword, canonical in SIGNAL_MAP.items():
        if keyword in query_lower:
            conditions.append({
                "key": "key_events_labels",
                "match": {"any": [canonical]},
            })

    # Identity filters
    if serial_number and camera_id:
        # Exact composite match
        conditions.append({
            "key": "scope_id",
            "match": {"value": f"{serial_number}:{camera_id}"},
        })
    elif serial_number:
        conditions.append({
            "key": "serial_number",
            "match": {"value": serial_number},
        })
    elif camera_id:
        # Non-unique convenience filter — may match across multiple trailers
        conditions.append({
            "key": "scope_id",
            "match": {"text": camera_id},
        })

    if not conditions:
        return None
    if len(conditions) == 1:
        return {"must": conditions}
    return {"must": conditions}


def _search_qdrant(vector, top_k: int, qdrant_filter: dict | None) -> list:
    url = f"{QDRANT_URL}/collections/{COLLECTION}/points/search"
    payload = {
        "vector": vector,
        "limit": top_k,
        "with_payload": True,
    }
    if qdrant_filter:
        payload["filter"] = qdrant_filter
    resp = httpx.post(url, json=payload, timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("result", [])


def query_summaries(
    text: str,
    top_k: int = 5,
    serial_number: str | None = None,
    camera_id: str | None = None,
):
    # 1. Expand and embed query
    expanded = _expand_query(text)
    embedding_client = EmbeddingClient()
    vector = embedding_client.embed(expanded)

    # 2. Search with pre-filter, fallback to unfiltered
    qdrant_filter = _build_filter(text, serial_number=serial_number, camera_id=camera_id)
    results = _search_qdrant(vector, top_k, qdrant_filter)
    filter_used = bool(qdrant_filter and results)

    if not results and qdrant_filter:
        # Fallback: no filtered results, try unfiltered
        results = _search_qdrant(vector, top_k, None)
        filter_used = False

    # 3. Print results
    if not results:
        print("No results found.")
        return

    print(f"\nQuery: {text}")
    if expanded != text:
        print(f"Expanded: {expanded}")
    if filter_used:
        print("Filter: key_events pre-filter applied")
    elif qdrant_filter:
        print("Filter: no matches, fell back to unfiltered search")
    print()

    for r in results:
        score = r.get("score")
        payload = r.get("payload", {}) or {}

        summary = payload.get("summary", "[no summary]")
        level = payload.get("level", "unknown")
        start_time = payload.get("start_time", "unknown")
        confidence = payload.get("confidence", "unknown")

        print(f"[score={score:.3f} level={level} time={start_time} conf={confidence}]")
        print(summary)
        print("-" * 80)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", type=str, help="Query text")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--serial-number", default=None, help="Filter by trailer serial number")
    parser.add_argument("--camera-id", default=None,
                        help="Filter by camera ID (non-unique without --serial-number)")

    args = parser.parse_args()

    query_summaries(
        args.query,
        args.top_k,
        serial_number=args.serial_number,
        camera_id=args.camera_id,
    )


if __name__ == "__main__":
    main()