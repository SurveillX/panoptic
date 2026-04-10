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


def _build_filter(text: str) -> dict | None:
    """Build a Qdrant filter matching normalized labels in key_events_labels."""
    query_lower = text.lower()
    conditions = []
    for keyword, canonical in SIGNAL_MAP.items():
        if keyword in query_lower:
            conditions.append({
                "key": "key_events_labels",
                "match": {"any": [canonical]},
            })
    if not conditions:
        return None
    if len(conditions) == 1:
        return {"must": conditions}
    # Multiple signals: require all
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


def query_summaries(text: str, top_k: int = 5):
    # 1. Expand and embed query
    expanded = _expand_query(text)
    embedding_client = EmbeddingClient()
    vector = embedding_client.embed(expanded)

    # 2. Search with pre-filter, fallback to unfiltered
    qdrant_filter = _build_filter(text)
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

    args = parser.parse_args()

    query_summaries(args.query, args.top_k)


if __name__ == "__main__":
    main()