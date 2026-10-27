"""Per-PDF facts for the audit inventory: document type (Layer 1) and extraction approach
tag (Layer 5), plus a copy of the page text in Postgres (page_text)."""
import re

from .pages import read_pages

PRICING_TABLE = re.compile(r"interest rates and interest charges|annual percentage rate \(apr\) for purchases", re.I)


def classify(file_name: str, text: str, page_count: int, content_type: str) -> str:
    name = file_name.lower()
    low = text[:20000].lower()
    if "privacy" in name or "what does" in low[:400] and "do with your personal information" in low:
        return "privacy_notice"
    if re.search(r"reward|cash ?back program|program terms|points", name) and not PRICING_TABLE.search(low):
        return "rewards_terms"
    if re.search(r"application|solicitation", name):
        return "application_disclosure"
    has_table = bool(PRICING_TABLE.search(low))
    agreement_words = bool(re.search(r"agreement|cardmember|cardholder|terms and conditions", name)) or \
        "this agreement" in low or "cardholder agreement" in low
    if has_table and (page_count or 0) <= 4 and not re.search(r"agreement", name):
        return "pricing_disclosure"
    if has_table and agreement_words and (page_count or 0) > 4:
        return "combined"
    if re.search(r"pricing|rates? and fees|rate sheet|schumer|addendum|disclosure|schedule", name):
        return "pricing_disclosure"
    if agreement_words:
        return "cardholder_agreement"
    return "other"


def approach(content_type: str, status_ok: bool = True) -> tuple[str, str]:
    if not status_ok:
        return "MANUAL_REVIEW", "file could not be read automatically"
    return {
        "scanned_images": ("OCR", "no usable text layer; every page read with GLM-OCR"),
        "mixed_text_images": ("HYBRID", "text layer for most pages + GLM-OCR for image-only pages"),
        "mixed_text_tables": ("TABLE_EXTRACT", "clean text layer; pricing in tables kept as markdown tables"),
        "pure_text": ("TEXT_EXTRACT", "clean text layer, no tables"),
    }.get(content_type, ("AI_ASSISTED", "layout not recognised; model reads the text"))


def sync_page_text(conn, sha256: str, md_path: str, ocr_model: str | None) -> int:
    """Replace the page_text rows of one document with the pages of its .md file."""
    pages = read_pages(md_path, "D1", sha256, "", "")
    conn.execute("DELETE FROM page_text WHERE sha256 = %s", (sha256,))
    for p in pages:
        conn.execute(
            "INSERT INTO page_text (sha256, page_no, source, model_version, text_md) VALUES (%s,%s,%s,%s,%s)",
            (sha256, p.page_no, "glm-ocr" if p.ocr else "pymupdf", ocr_model if p.ocr else None,
             p.text.replace("\x00", "")))
    return len(pages)
