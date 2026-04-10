from shared.clients.vector_store import VectorStore
from shared.clients.qdrant import upsert_point


class QdrantVectorStore(VectorStore):
    def upsert(self, summary_id: str, vector: list[float], payload: dict) -> None:
        upsert_point(summary_id, vector, payload)
