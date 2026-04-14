import argparse

from shared.clients.embedding import EmbeddingClient
from shared.clients.qdrant import search_summaries
from shared.search.keyword_expansion import expand_query, extract_canonical_labels


def _build_filter(
    text: str,
    serial_number: str | None = None,
    camera_id: str | None = None,
) -> dict | None:
    """Build a Qdrant filter matching normalized labels in key_events_labels.

    Exact-match semantics only. `camera_id` without `serial_number` is rejected
    here because scope_id is "{serial_number}:{camera_id}" — matching on
    camera_id alone would require substring semantics, which we don't allow.
    """
    conditions: list[dict] = []

    labels = extract_canonical_labels(text)
    if labels:
        conditions.append({"key": "key_events_labels", "match": {"any": labels}})

    if serial_number:
        conditions.append({"key": "serial_number", "match": {"value": serial_number}})
        if camera_id:
            conditions.append({
                "key": "scope_id",
                "match": {"value": f"{serial_number}:{camera_id}"},
            })
    elif camera_id:
        raise ValueError("--camera-id requires --serial-number (no substring matching)")

    return {"must": conditions} if conditions else None


def query_summaries(
    text: str,
    top_k: int = 5,
    serial_number: str | None = None,
    camera_id: str | None = None,
):
    # 1. Expand and embed query
    expanded = expand_query(text)
    embedding_client = EmbeddingClient()
    vector = embedding_client.embed(expanded)

    # 2. Search with pre-filter, fallback to unfiltered
    qdrant_filter = _build_filter(text, serial_number=serial_number, camera_id=camera_id)
    results = search_summaries(vector, qdrant_filter, top_k)
    filter_used = bool(qdrant_filter and results)

    if not results and qdrant_filter:
        # Fallback: no filtered results, try unfiltered
        results = search_summaries(vector, None, top_k)
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