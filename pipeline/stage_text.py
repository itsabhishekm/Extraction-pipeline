"""Stage 2 - text: read PDF metadata, classify every page, write the .md file.

Each page is either read from the PDF's embedded text (PyMuPDF / pymupdf4llm)
or marked for OCR. A page needs OCR when its text layer is garbled, or when it
has little text AND either images cover part of it or its letters are drawn as
vector shapes ("Print to PDF" output). A short page with neither (e.g. "This
page intentionally left blank") stays a text page.

Output: <data_root>/text/<issuer folder>/<file name>.md with one marker per page:
    <!-- page 3 -->               text follows
    <!-- page 4: needs_ocr -->    filled in later by the OCR stage
Documents with OCR pages move to stage 'needs_ocr', the rest to 'text'.
"""
import re
import unicodedata
from pathlib import Path

import pymupdf
import pymupdf4llm

from .config import Config, progress, setup_logging
from .db import connect
from .docinfo import approach, classify, sync_page_text

WORD = re.compile(r"[A-Za-zÀ-ſ]{3,}")
ES_WORDS = {"de", "la", "el", "que", "los", "las", "por", "para", "con", "su", "tarjeta", "cuenta", "pago"}
EN_WORDS = {"the", "and", "you", "your", "to", "of", "for", "account", "card", "payment"}


def image_cover(page: pymupdf.Page) -> float:
    """Share of the page area covered by images (overlaps may count twice; capped at 1)."""
    area = abs(page.rect)
    if not area:
        return 0.0
    total = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        total += abs(r)
    return min(total / area, 1.0)


def bad_char_share(text: str) -> float:
    """Share of characters that are unreadable: replacement chars, private-use or unassigned code points."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for c in chars if c == "�" or unicodedata.category(c) in ("Co", "Cn", "Cc"))
    bad += 5 * len(re.findall(r"\(cid:\d+\)", text))  # unmapped glyphs from broken fonts
    return min(bad / len(chars), 1.0)


def classify_page(page: pymupdf.Page, t: dict) -> tuple[bool, str]:
    """Return (needs_ocr, reason)."""
    text = page.get_text("text")
    chars = len(text.strip())
    words = len(WORD.findall(text))
    cover = image_cover(page)
    if chars and bad_char_share(text) > t["garbled_max_bad"]:
        return True, "garbled text layer"
    little_text = chars < t["min_chars"] or words < t["min_words"]
    if not little_text:
        return False, "text layer"
    if cover >= t["image_cover_for_ocr"]:
        return True, f"little text ({chars} chars), images cover {cover:.0%}"
    # "Print to PDF" and some converters draw each letter as vector shapes: no text, no images.
    shapes = len(page.get_drawings())
    if shapes >= t["min_drawings_for_ocr"]:
        return True, f"little text ({chars} chars), text drawn as {shapes} vector shapes"
    return False, "short text page"


def guess_languages(text: str) -> list[str]:
    tokens = re.findall(r"[a-záéíóúñ]+", text.lower())
    es = sum(t in ES_WORDS for t in tokens)
    en = sum(t in EN_WORDS for t in tokens)
    langs = []
    if en >= 20 or (en and en >= es):
        langs.append("en")
    if es >= 20 and es > en * 0.5:
        langs.append("es")
    return langs or ["unknown"]


def page_markdown(doc: pymupdf.Document) -> list[str]:
    """Markdown per page (tables kept as markdown tables). Falls back to plain text."""
    try:
        chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, use_ocr=False, show_progress=False)
        if len(chunks) == doc.page_count:
            return [c["text"] for c in chunks]
    except Exception:  # noqa: BLE001 - any layout failure falls back to plain text
        pass
    return [p.get_text("text") for p in doc]


def md_path_for(cfg: Config, folder: str, file_name: str) -> Path:
    return cfg.text_dir / folder / f"{file_name}.md"


def process(cfg: Config, row: dict) -> dict:
    src = cfg.input_root / row["folder_name"] / row["file_name"]
    t = cfg.section("text")
    with src.open("rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:  # e.g. an HTML error page saved with a .pdf name
        raise ValueError(f"not a PDF file (starts with {head[:40]!r})")
    doc = pymupdf.open(src, filetype="pdf")
    if doc.needs_pass:
        raise ValueError("PDF is password protected")
    # Postgres text cannot hold NUL (0x00) bytes; some PDFs carry them in metadata or text
    meta = {k: (v.replace("\x00", "") if isinstance(v, str) else v) for k, v in (doc.metadata or {}).items()}

    ocr_pages, md_parts, reasons, plain = [], [], [], []
    md_pages = [m.replace("\x00", "") for m in page_markdown(doc)]
    for i, page in enumerate(doc, start=1):
        needs_ocr, reason = classify_page(page, t)
        if needs_ocr:
            ocr_pages.append(i)
            reasons.append(f"page {i}: {reason}")
            md_parts.append(f"<!-- page {i}: needs_ocr -->\n")
        else:
            md_parts.append(f"<!-- page {i} -->\n{md_pages[i - 1].strip()}\n")
            plain.append(page.get_text("text").replace("\x00", ""))

    n = doc.page_count
    header = (f"<!-- source: {row['folder_name']} / {row['file_name']} -->\n"
              f"<!-- sha256: {row['sha256']} | pages: {n} | ocr pages: {ocr_pages or 'none'} -->\n\n")
    out = md_path_for(cfg, row["folder_name"], row["file_name"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + "\n".join(md_parts), encoding="utf-8")

    body = "\n".join(md_pages[i - 1] for i in range(1, n + 1) if i not in ocr_pages)
    has_tables = "|---" in body or "| ---" in body
    if not ocr_pages:
        content_type, source = ("mixed_text_tables" if has_tables else "pure_text"), "pymupdf"
    elif len(ocr_pages) == n:
        content_type, source = "scanned_images", "glm-ocr"
    else:
        content_type, source = "mixed_text_images", "mixed"
    tag, tag_reason = approach(content_type)

    return {
        "document_type": classify(row["file_name"], body, n, content_type),
        "extraction_tag": tag,
        "extraction_tag_reason": tag_reason,
        "page_count": n,
        "file_size_kb": round(src.stat().st_size / 1024, 1),
        "pdf_version": meta.get("format") or None,
        "producer": meta.get("producer") or meta.get("creator") or None,
        "pdf_created": meta.get("creationDate") or None,
        "pdf_modified": meta.get("modDate") or None,
        "encrypted": bool(doc.is_encrypted),
        "languages": guess_languages("\n".join(plain)) if plain else ["unknown"],
        "content_type": content_type,
        "text_source": source,
        "md_path": str(out),
        "text_pages": n - len(ocr_pages),
        "ocr_pages": ocr_pages,
        "layout_notes": reasons,
        "stage": "needs_ocr" if ocr_pages else "text",
    }


def run(cfg: Config, retry_failed: bool = False) -> dict:
    log = setup_logging(cfg, "text")
    counts = {"processed": 0, "text_only": 0, "needs_ocr": 0, "ocr_pages": 0, "failed": 0}
    status_filter = "status IN ('ok','failed')" if retry_failed else "status = 'ok'"
    with connect(cfg.database_url) as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT ON (d.sha256) d.sha256, f.file_name, i.folder_name
                FROM documents d
                JOIN document_files f ON f.sha256 = d.sha256
                JOIN issuers i ON i.issuer_id = f.issuer_id
                WHERE d.stage = 'scanned' AND d.{status_filter}
                ORDER BY d.sha256, f.file_id"""
        ).fetchall()
        log.info("%d documents waiting for text extraction", len(rows))
        progress.start(len(rows), "PDFs")
        for n, row in enumerate(rows, start=1):
            progress.step(n, row["folder_name"][:30])
            try:
                r = process(cfg, row)
                conn.execute(
                    """UPDATE documents SET page_count=%(page_count)s, file_size_kb=%(file_size_kb)s,
                         pdf_version=%(pdf_version)s, producer=%(producer)s, pdf_created=%(pdf_created)s,
                         pdf_modified=%(pdf_modified)s, encrypted=%(encrypted)s, languages=%(languages)s,
                         content_type=%(content_type)s, text_source=%(text_source)s, md_path=%(md_path)s,
                         text_pages=%(text_pages)s, ocr_pages=%(ocr_pages)s, layout_notes=%(layout_notes)s,
                         document_type=%(document_type)s, extraction_tag=%(extraction_tag)s,
                         extraction_tag_reason=%(extraction_tag_reason)s,
                         stage=%(stage)s, status='ok', last_error=NULL, errors='{}', updated_at=now()
                       WHERE sha256=%(sha256)s""",
                    {**r, "sha256": row["sha256"]},
                )
                sync_page_text(conn, row["sha256"], r["md_path"], None)
                counts["processed"] += 1
                counts["needs_ocr" if r["ocr_pages"] else "text_only"] += 1
                counts["ocr_pages"] += len(r["ocr_pages"])
                log.info("%s / %s: %d pages, OCR pages %s", row["folder_name"],
                         row["file_name"], r["page_count"], r["ocr_pages"] or "none")
            except Exception as e:  # noqa: BLE001 - record the failure and keep going
                conn.rollback()
                err = f"{type(e).__name__}: {e}"[:1000]
                tag, tag_reason = approach("", status_ok=False)
                conn.execute(
                    """UPDATE documents SET status='failed', last_error=%s, errors=array_append(errors, %s),
                         extraction_tag=%s, extraction_tag_reason=%s, updated_at=now() WHERE sha256=%s""",
                    (err, err, tag, tag_reason, row["sha256"]),
                )
                counts["failed"] += 1
                log.error("FAILED %s / %s: %s", row["folder_name"], row["file_name"], e)
            conn.commit()
    progress.stop()
    log.info("text done: %s", counts)
    return counts
