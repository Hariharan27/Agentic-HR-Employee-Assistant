from qdrant_client import QdrantClient, models

from app.core.exceptions import ApplicationError
from app.rag.models import PolicyChunk, PolicySearchResult


class VectorStoreUnavailableError(ApplicationError):
    status_code = 503
    code = "vector_store_unavailable"


class QdrantPolicyVectorStore:
    def __init__(self, *, collection: str, vector_size: int = 384,
                 url: str | None = None, client: QdrantClient | None = None):
        self.collection = collection
        self.vector_size = vector_size
        self.client = client or QdrantClient(url=url)

    def ensure_collection(self) -> None:
        try:
            if not self.client.collection_exists(self.collection):
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(size=self.vector_size, distance=models.Distance.COSINE),
                )
        except Exception as exc:
            raise VectorStoreUnavailableError("Policy vector store is unavailable") from exc

    def replace_document(self, document: str, chunks: list[PolicyChunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("Each policy chunk must have one embedding")
        try:
            self.ensure_collection()
            self.client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(filter=models.Filter(must=[
                    models.FieldCondition(key="document", match=models.MatchValue(value=document))
                ])),
                wait=True,
            )
            if chunks:
                self.client.upsert(
                    collection_name=self.collection,
                    points=[models.PointStruct(id=chunk.id, vector=vector,
                             payload={"text": chunk.text, **chunk.metadata}) for chunk, vector in zip(chunks, vectors)],
                    wait=True,
                )
        except VectorStoreUnavailableError: raise
        except Exception as exc:
            raise VectorStoreUnavailableError("Failed to update the policy vector store") from exc

    def search(self, vector: list[float], *, top_k: int, score_threshold: float) -> list[PolicySearchResult]:
        try:
            self.ensure_collection()
            points = self.client.query_points(
                collection_name=self.collection, query=vector, limit=top_k,
                score_threshold=score_threshold, with_payload=True,
            ).points
            return [PolicySearchResult(
                text=str(point.payload["text"]), score=float(point.score),
                document=str(point.payload["document"]), page=int(point.payload["page"]),
                section=str(point.payload["section"]), category=str(point.payload["category"]),
            ) for point in points]
        except VectorStoreUnavailableError: raise
        except Exception as exc:
            raise VectorStoreUnavailableError("Failed to search the policy vector store") from exc
