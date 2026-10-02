import argparse
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.embeddings.bge import BGEEmbeddingProvider
from app.infrastructure.vector_store.qdrant import QdrantPolicyVectorStore
from app.rag.chunking import chunk_pages
from app.rag.extraction import extract_pdf_pages


def ingest_policy_documents(policy_directory: Path) -> tuple[int, int, int]:
    settings = get_settings()
    pdf_paths = sorted(policy_directory.glob("*.pdf"))
    if not pdf_paths:
        raise FileNotFoundError(f"No policy PDFs found in {policy_directory}")

    embeddings = BGEEmbeddingProvider(settings.embedding_model)
    vector_store = QdrantPolicyVectorStore(
        collection=settings.qdrant_collection,
        vector_size=settings.rag_collection_vector_size,
        url=settings.qdrant_url,
    )

    page_count = 0
    chunk_count = 0
    for pdf_path in pdf_paths:
        pages = extract_pdf_pages(pdf_path)
        chunks = chunk_pages(
            pages,
            chunk_size=settings.rag_chunk_size,
            overlap=settings.rag_chunk_overlap,
        )
        vectors = embeddings.embed_documents([chunk.text for chunk in chunks])
        vector_store.replace_document(pdf_path.name, chunks, vectors)
        page_count += len(pages)
        chunk_count += len(chunks)
        print(f"Indexed {pdf_path.name}: {len(pages)} pages, {len(chunks)} chunks")

    return len(pdf_paths), page_count, chunk_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Index HR policy PDFs into Qdrant")
    parser.add_argument("--directory", type=Path, help="Override the configured policy directory")
    args = parser.parse_args()
    settings = get_settings()
    directory = args.directory or Path(settings.policy_docs_dir)
    documents, pages, chunks = ingest_policy_documents(directory)
    print(f"Policy ingestion complete: {documents} documents, {pages} pages, {chunks} chunks")


if __name__ == "__main__":
    main()
