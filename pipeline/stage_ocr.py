"""Stage 3 - ocr: read the scanned pages with GLM-OCR and fill them into the .md file.

Takes documents at stage 'needs_ocr'. Each marked page is rendered to an image and
sent to GLM-OCR; its placeholder in the .md file
    <!-- page 4: needs_ocr -->
becomes
    <!-- page 4: ocr -->
    <text read by GLM-OCR>
Each page is written to the file as soon as it is read, so a crash only loses the
current page. The document then moves to stage 'text'.
"""
import re
from pathlib import Path

import pymupdf

from . import llm
from .docinfo import classify, sync_page_text
from .config import Config, progress, setup_logging
from .db import connect, finish_run, start_run


def fill_page(md: str, page_no: int, text: str) -> str:
    marker = f"<!-- page {page_no}: needs_ocr -->\n"
    if marker not in md:
        return md
    return md.replace(marker, f"<!-- page {page_no}: ocr -->\n{text.strip()}\n", 1)


def pending_pages(md: str) -> list[int]:
    return [int(n) for n in re.findall(r"<!-- page (\d+): needs_ocr -->", md)]


def run(cfg: Config) -> dict:
    log = setup_logging(cfg, "ocr")
    model = cfg.section("models").get("ocr", "glm-ocr")
    dpi = cfg.section("ocr").get("dpi", 150)
    counts = {"documents": 0, "pages": 0, "failed": 0}

    with connect(cfg.database_url) as conn:
        run_id = start_run(conn, cfg, "ocr")
        llm.configure(cfg, "ocr", run_id)
        rows = conn.execute(
            """SELECT DISTINCT ON (d.sha256) d.sha256, d.md_path, d.ocr_pages, f.file_name, i.folder_name
               FROM documents d
               JOIN document_files f ON f.sha256 = d.sha256
               JOIN issuers i ON i.issuer_id = f.issuer_id
               WHERE d.stage = 'needs_ocr' AND d.status = 'ok'
               ORDER BY d.sha256, f.file_id""").fetchall()
        total_pages = sum(len(r["ocr_pages"]) for r in rows)
        log.info("%d documents, %d pages waiting for OCR (model %s)", len(rows), total_pages, model)

        progress.start(total_pages, "pages")
        page_i = 0
        for n, row in enumerate(rows, start=1):
            src = cfg.input_root / row["folder_name"] / row["file_name"]
            try:
                md_file = Path(row["md_path"])
                md = md_file.read_text(encoding="utf-8")
                doc = pymupdf.open(src, filetype="pdf")
                for page_no in pending_pages(md):
                    page_i += 1
                    progress.step(page_i, f"doc {n}/{len(rows)}")
                    png = doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png")
                    text = llm.ocr_page(model, png, meta={"file": row["file_name"], "page": page_no})
                    md = fill_page(md, page_no, text or "[OCR returned no text]")
                    md_file.write_text(md, encoding="utf-8")
                    counts["pages"] += 1
                    log.info("%s / %s page %d: %d chars", row["folder_name"],
                             row["file_name"], page_no, len(text))
                md = md.replace("| ocr pages: ", "| ocr pages (read by GLM-OCR): ", 1)
                md_file.write_text(md, encoding="utf-8")
                doc_type = classify(row["file_name"], md, len(doc), "")
                conn.execute("UPDATE documents SET stage='text', document_type=COALESCE(%s, document_type), "
                             "updated_at=now() WHERE sha256=%s", (doc_type, row["sha256"]))
                sync_page_text(conn, row["sha256"], str(md_file), model)
                counts["documents"] += 1
            except Exception as e:  # noqa: BLE001 - record and continue with the next document
                conn.rollback()
                conn.execute("UPDATE documents SET status='failed', last_error=%s, updated_at=now() WHERE sha256=%s",
                             (f"OCR: {type(e).__name__}: {e}"[:1000], row["sha256"]))
                counts["failed"] += 1
                log.error("FAILED %s / %s: %s", row["folder_name"], row["file_name"], e)
            conn.commit()
        finish_run(conn, run_id)

    llm.unload(model)
    progress.stop()
    log.info("ocr done: %s", counts)
    return counts
