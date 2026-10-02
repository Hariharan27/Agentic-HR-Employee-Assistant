from dataclasses import dataclass

from app.core.exceptions import MissingPolicyEvidenceError, ValidationError
from app.rag.models import PolicySearchResult
from app.rag.ports import EmbeddingProvider, PolicyVectorStore


@dataclass(frozen=True, slots=True)
class PolicyContext:
    text: str
    sources: list[dict[str, object]]
    matches: list[PolicySearchResult]


class PolicyKnowledgeService:
    def __init__(
        self,
        embeddings: EmbeddingProvider,
        vector_store: PolicyVectorStore,
        *,
        top_k: int = 4,
        score_threshold: float = 0.45,
    ):
        self.embeddings = embeddings
        self.vector_store = vector_store
        self.top_k = top_k
        self.score_threshold = score_threshold

    def search(self, question: str) -> PolicyContext:
        normalized = question.strip()
        if not normalized:
            raise ValidationError("A policy question is required")

        query_vector = self.embeddings.embed_query(normalized)
        matches = self.vector_store.search(
            query_vector,
            top_k=self.top_k,
            score_threshold=self.score_threshold,
        )
        if not matches:
            raise MissingPolicyEvidenceError(
                "The policy library does not contain enough evidence to answer this question"
            )

        context_blocks = [
            (
                f"[Source: {match.document}, page {match.page}, section: {match.section}]\n"
                f"{match.text}"
            )
            for match in matches
        ]
        return PolicyContext(
            text="\n\n".join(context_blocks),
            sources=[match.source for match in matches],
            matches=matches,
        )
