from uuid import NAMESPACE_URL, uuid5

from app.rag.extraction import detect_section
from app.rag.models import PolicyChunk, PolicyPage


def chunk_pages(pages: list[PolicyPage], *, chunk_size: int = 1600, overlap: int = 250) -> list[PolicyChunk]:
    if chunk_size < 200: raise ValueError("chunk_size must be at least 200 characters")
    if overlap < 0 or overlap >= chunk_size: raise ValueError("overlap must be between 0 and chunk_size")
    chunks: list[PolicyChunk] = []
    for page in pages:
        start = 0
        index = 0
        while start < len(page.text):
            end = min(start + chunk_size, len(page.text))
            if end < len(page.text):
                split = page.text.rfind("\n", start + chunk_size // 2, end)
                if split < 0: split = page.text.rfind(" ", start + chunk_size // 2, end)
                if split > start: end = split
            text = page.text[start:end].strip()
            if text:
                identity = f"{page.document}|{page.page}|{index}|{text}"
                section = detect_section(text, page.section)
                chunks.append(
                    PolicyChunk(
                        str(uuid5(NAMESPACE_URL, identity)),
                        text,
                        page.document,
                        page.page,
                        section,
                        page.category,
                        index,
                    )
                )
                index += 1
            if end >= len(page.text): break
            start = max(end - overlap, start + 1)
    return chunks
