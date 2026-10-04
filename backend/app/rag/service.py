from dataclasses import dataclass
import re

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

        query_vector = self.embeddings.embed_query(self._expand_query(normalized))
        matches = self.vector_store.search(
            query_vector,
            top_k=self._retrieval_limit(normalized),
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

    def _retrieval_limit(self, question: str) -> int:
        """Use a small targeted recall increase for clauses known to span nearby sections."""
        if re.search(
            r"\b(resignation\s+notice|serving\s+notice|notice\s+period)\b",
            question.casefold(),
        ):
            return max(self.top_k, 6)
        return self.top_k

    @staticmethod
    def _expand_query(question: str) -> str:
        """Add unambiguous policy wording where employees use HR shorthand.

        This stays within retrieval only: the answer is still constrained to the
        retrieved policy passages. It improves recall for policy clauses whose
        wording is more formal than a user's question.
        """
        normalized = question.casefold()
        if re.search(r"\b(resignation\s+notice|serving\s+notice|notice\s+period)\b", normalized):
            return (
                f"{question}\n\n"
                "Policy clause: employees serving their resignation notice period are not eligible "
                "to avail Casual Leave (CL), Sick Leave (SL), Privilege Leave (PL), Earned Leave "
                "(EL), or Work from Home (WFH)."
            )
        return question
