class VectorStore:
    def upsert(self, summary_id: str, vector: list[float], payload: dict) -> None:
        raise NotImplementedError
