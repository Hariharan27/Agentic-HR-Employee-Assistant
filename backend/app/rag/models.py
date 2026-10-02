from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PolicyPage:
    document: str
    page: int
    text: str
    section: str
    category: str


@dataclass(frozen=True, slots=True)
class PolicyChunk:
    id: str
    text: str
    document: str
    page: int
    section: str
    category: str
    chunk_index: int

    @property
    def metadata(self) -> dict[str, Any]:
        return {"document": self.document, "page": self.page, "section": self.section,
                "category": self.category, "chunk_index": self.chunk_index}


@dataclass(frozen=True, slots=True)
class PolicySearchResult:
    text: str
    score: float
    document: str
    page: int
    section: str
    category: str

    @property
    def source(self) -> dict[str, Any]:
        return {"document": self.document, "page": self.page, "section": self.section,
                "category": self.category, "score": round(self.score, 4)}

