import pytest
from PIL import Image, ImageDraw
from qdrant_client import QdrantClient

from app.core.exceptions import MissingPolicyEvidenceError, ValidationError
from app.infrastructure.vector_store.qdrant import QdrantPolicyVectorStore
from app.rag.chunking import chunk_pages
from app.rag.extraction import _is_visually_blank, clean_text, detect_section, infer_category
from app.rag.models import PolicyChunk, PolicyPage, PolicySearchResult
from app.rag.service import PolicyKnowledgeService


class FakeEmbeddings:
    def __init__(self):
        self.query = None

    def embed_query(self, text: str) -> list[float]:
        self.query = text
        return [1.0, 0.0, 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeVectorStore:
    def __init__(self, results: list[PolicySearchResult]):
        self.results = results
        self.search_arguments = None

    def ensure_collection(self) -> None:
        return None

    def replace_document(self, document, chunks, vectors) -> None:
        return None

    def search(self, vector, *, top_k, score_threshold):
        self.search_arguments = (vector, top_k, score_threshold)
        return self.results


def test_clean_text_normalizes_ocr_output_and_dehyphenates_words():
    raw = "Annual leave frame-\nwork\x00\n\n\nAnnual   entitlement\nAnnual   entitlement"

    assert clean_text(raw) == "Annual leave framework\n\nAnnual entitlement"


def test_policy_category_is_inferred_from_document_name():
    assert infer_category("Revised Leave Policy.pdf") == "leave"
    assert infer_category("Information Security Policy.pdf") == "security"


def test_blank_page_detection_does_not_hide_real_content():
    blank = Image.new("L", (1000, 1000), 255)
    content = blank.copy()
    ImageDraw.Draw(content).rectangle((100, 100, 900, 200), fill=0)

    assert _is_visually_blank(blank) is True
    assert _is_visually_blank(content) is False


def test_numbered_section_is_preferred_over_table_header():
    text = "Leave Category Quarterly Credit Leave Credit Status\n4. Types of Leave and Entitlements"

    assert detect_section(text, "Leave Policy") == "4. Types of Leave and Entitlements"


def test_chunking_preserves_source_metadata_and_is_deterministic():
    page = PolicyPage(
        document="Leave Policy.pdf",
        page=2,
        text="Casual leave is six days. " * 30,
        section="Casual Leave",
        category="leave",
    )

    first = chunk_pages([page], chunk_size=220, overlap=40)
    second = chunk_pages([page], chunk_size=220, overlap=40)

    assert len(first) > 1
    assert [chunk.id for chunk in first] == [chunk.id for chunk in second]
    assert first[0].metadata == {
        "document": "Leave Policy.pdf",
        "page": 2,
        "section": "Casual Leave",
        "category": "leave",
        "chunk_index": 0,
    }


def test_qdrant_replaces_document_and_returns_source_metadata():
    client = QdrantClient(location=":memory:")
    store = QdrantPolicyVectorStore(
        collection="test-policies",
        vector_size=3,
        client=client,
    )
    old = PolicyChunk("d54a97bb-f4bb-4bbc-9e96-d63f4e55c13c", "old policy", "Leave.pdf", 1,
                      "Leave", "leave", 0)
    new = PolicyChunk("b313c214-01ea-4459-a972-c04995e8d1fe", "six casual leave days", "Leave.pdf", 2,
                      "Casual Leave", "leave", 0)

    store.replace_document("Leave.pdf", [old], [[0.0, 1.0, 0.0]])
    store.replace_document("Leave.pdf", [new], [[1.0, 0.0, 0.0]])
    results = store.search([1.0, 0.0, 0.0], top_k=3, score_threshold=0.5)

    assert len(results) == 1
    assert results[0].text == "six casual leave days"
    assert results[0].source["document"] == "Leave.pdf"
    assert results[0].source["page"] == 2
    client.close()


def test_policy_service_builds_grounded_context_with_sources():
    match = PolicySearchResult(
        text="Employees receive six casual leave days.",
        score=0.91,
        document="Leave Policy.pdf",
        page=2,
        section="Casual Leave",
        category="leave",
    )
    vector_store = FakeVectorStore([match])
    service = PolicyKnowledgeService(FakeEmbeddings(), vector_store, top_k=4, score_threshold=0.45)

    context = service.search("How many casual leave days do I get?")

    assert "Leave Policy.pdf, page 2" in context.text
    assert "six casual leave days" in context.text
    assert context.sources == [match.source]
    assert vector_store.search_arguments == ([1.0, 0.0, 0.0], 4, 0.45)


def test_policy_service_rejects_empty_question():
    service = PolicyKnowledgeService(FakeEmbeddings(), FakeVectorStore([]))

    with pytest.raises(ValidationError, match="question is required"):
        service.search("  ")


def test_policy_service_refuses_to_answer_without_evidence():
    service = PolicyKnowledgeService(FakeEmbeddings(), FakeVectorStore([]))

    with pytest.raises(MissingPolicyEvidenceError, match="enough evidence"):
        service.search("What is the moon leave allowance?")


def test_policy_service_expands_resignation_notice_question_for_retrieval():
    embeddings = FakeEmbeddings()
    match = PolicySearchResult(
        text="Employees serving notice are not eligible for leave.", score=0.91,
        document="Leave Policy.pdf", page=3, section="Leave", category="leave",
    )
    service = PolicyKnowledgeService(embeddings, FakeVectorStore([match]))

    service.search("Can an employee serving resignation notice use CL or WFH?")

    assert "not eligible to avail Casual Leave" in embeddings.query
    assert "Work from Home (WFH)" in embeddings.query
    assert service.vector_store.search_arguments[1] == 6
