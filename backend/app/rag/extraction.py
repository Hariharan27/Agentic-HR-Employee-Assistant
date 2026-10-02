import re
import unicodedata
from io import BytesIO
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pypdf import PdfReader
from pytesseract import Output

from app.core.exceptions import ValidationError
from app.rag.models import PolicyPage


HEADING_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9 /&,'().:-]{2,100}$")
NUMBERED_HEADING_PATTERN = re.compile(r"^\d+(?:\.\d+)*[.)]?\s+[A-Z].{2,100}$")


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned: list[str] = []
    for line in lines:
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if not cleaned or line != cleaned[-1]:
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def infer_category(filename: str) -> str:
    name = filename.lower()
    if "leave" in name or "holiday" in name or "benefit" in name:
        return "leave"
    if "onboard" in name or "registration" in name or "access allocation" in name:
        return "onboarding"
    if any(
        term in name
        for term in ("security", "password", "malware", "encryption", "backup", "vulnerability")
    ):
        return "security"
    if "vendor" in name:
        return "vendor"
    return "hr"


def detect_section(text: str, fallback: str) -> str:
    candidates: list[str] = []
    for line in text.splitlines():
        candidate = re.sub(r"^[•*-]\s*", "", line).strip().rstrip(":")
        if (
            HEADING_PATTERN.fullmatch(candidate) or NUMBERED_HEADING_PATTERN.fullmatch(candidate)
        ) and len(candidate.split()) <= 12:
            candidates.append(candidate)
    for candidate in candidates:
        if NUMBERED_HEADING_PATTERN.fullmatch(candidate):
            return candidate
    for candidate in candidates:
        normalized = candidate.lower()
        if "policy" in normalized or "holiday list" in normalized or "procedure" in normalized:
            return candidate
    if candidates:
        return candidates[0]
    return fallback


def _prepare_image(page: pymupdf.Page, dpi: int) -> Image.Image:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB)
    image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("L")
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(1.15)
    return image.filter(ImageFilter.SHARPEN)


def _is_visually_blank(image: Image.Image) -> bool:
    histogram = image.histogram()
    dark_pixels = sum(histogram[:240])
    total_pixels = image.width * image.height
    return total_pixels > 0 and dark_pixels / total_pixels < 0.0001


def _ocr_candidate(image: Image.Image, page_segmentation_mode: int) -> tuple[str, float]:
    data = pytesseract.image_to_data(
        image, lang="eng", config=f"--oem 3 --psm {page_segmentation_mode}", output_type=Output.DICT,
    )
    words: list[str] = []
    confidences: list[float] = []
    current_line: tuple[int, int, int] | None = None
    lines: list[str] = []
    line_words: list[str] = []
    for index, raw_text in enumerate(data["text"]):
        word = raw_text.strip()
        if not word: continue
        line_key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        if current_line is not None and line_key != current_line and line_words:
            lines.append(" ".join(line_words)); line_words = []
        current_line = line_key
        line_words.append(word); words.append(word)
        confidence = float(data["conf"][index])
        if confidence >= 0: confidences.append(confidence)
    if line_words: lines.append(" ".join(line_words))
    average_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n".join(lines), average_confidence


def _ocr_page(
    document: pymupdf.Document,
    page_number: int,
    dpi: int = 300,
) -> tuple[str, float, bool]:
    try:
        image = _prepare_image(document[page_number - 1], dpi)
        if _is_visually_blank(image):
            return "", 0.0, True
        automatic = _ocr_candidate(image, 3)
        # Retry dense tables/blocks when automatic layout confidence is weak.
        if automatic[1] >= 82 and len(automatic[0]) >= 100:
            return *automatic, False
        uniform = _ocr_candidate(image, 6)
        best = max((automatic, uniform), key=lambda item: (item[1], len(item[0])))
        return *best, False
    except pytesseract.TesseractNotFoundError as exc:
        raise ValidationError("OCR requires the tesseract executable") from exc


def extract_pdf_pages(pdf_path: Path, *, ocr_min_characters: int = 80) -> list[PolicyPage]:
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValidationError(f"Policy PDF was not found: {pdf_path}")
    reader = PdfReader(str(pdf_path))
    rendered = pymupdf.open(str(pdf_path))
    pages: list[PolicyPage] = []
    current_section = pdf_path.stem
    try:
        for index, page in enumerate(reader.pages, start=1):
            extracted = clean_text(page.extract_text() or "")
            if len(extracted) < ocr_min_characters:
                raw_ocr, confidence, is_blank = _ocr_page(rendered, index)
                if is_blank:
                    continue
                extracted = clean_text(raw_ocr)
                if confidence < 45:
                    raise ValidationError(
                        f"OCR confidence is too low for {pdf_path.name}, page {index} "
                        f"({confidence:.1f})"
                    )
            if not extracted:
                raise ValidationError(f"No readable text found in {pdf_path.name}, page {index}")
            current_section = detect_section(extracted, current_section)
            pages.append(
                PolicyPage(
                    pdf_path.name,
                    index,
                    extracted,
                    current_section,
                    infer_category(pdf_path.name),
                )
            )
        return pages
    finally:
        rendered.close()
