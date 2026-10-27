"""Human Review: the values the Repair Agent could not fix (review_items), including conflicts.

    python run.py review                 # go through open items one by one in the terminal
    python run.py review --export f.csv  # write open items to a CSV (fill the decision columns in Excel)
    python run.py review --import f.csv  # apply the decisions from that CSV

Decisions
    approve   the extracted value is right                       -> validation human_approved
    correct   give the right status / value / quote / page        -> value replaced, human_approved
    reject    the value cannot be determined from the PDFs        -> status ambiguous, human_approved
    skip      leave it for later
A conflict is resolved by approving the value you keep (or correcting it); the conflicts row
records the decision. After each decision the card JSON is rewritten.
"""
import csv
import getpass
import json

from .config import Config, setup_logging
from .db import connect
from .json_out import write_card
from .pages import read_pages
from .postprocess import display_for
from .stage_extract import load_fields
from .stage_validate import finish_version, quote_check

STATUSES = ["found", "stated_none", "not_applicable", "not_present", "unreadable", "ambiguous"]
COLUMNS = ["review_id", "issuer", "card", "version_id", "field", "reason", "status", "raw_value",
           "normalized_value", "quote", "source_pdf", "page", "decision", "new_status", "new_raw_value",
           "new_normalized_value", "new_quote", "new_source_pdf", "new_page", "comment"]


def open_items(conn, quarter):
    return conn.execute(
        """SELECT r.review_id, r.reason, fv.*, i.folder_name AS issuer, c.card_name, c.card_id,
                  f.file_name AS source_pdf
           FROM review_items r JOIN field_values fv USING (value_id)
           JOIN card_versions v USING (version_id) JOIN cards c USING (card_id) JOIN issuers i USING (issuer_id)
           LEFT JOIN LATERAL (SELECT file_name FROM document_files x WHERE x.sha256 = fv.source_sha256
                              ORDER BY file_id LIMIT 1) f ON true
           WHERE r.decision IS NULL AND v.quarter = %s ORDER BY i.folder_name, c.card_name, fv.field_name""",
        (quarter,)).fetchall()


def version_files(conn, version_id):
    return conn.execute(
        """SELECT vd.sha256, d.md_path, d.page_count, f.file_name FROM version_documents vd
           JOIN documents d USING (sha256)
           JOIN LATERAL (SELECT file_name FROM document_files x WHERE x.sha256 = vd.sha256
                         ORDER BY file_id LIMIT 1) f ON true WHERE vd.version_id=%s""", (version_id,)).fetchall()


def apply_decision(conn, cfg, item, decision, reviewer, new=None, comment=""):
    """new: dict(status, raw_value, normalized_value, quote, source_pdf, page) for 'correct'.
    Returns a list of warnings (the decision is applied anyway, except on hard errors)."""
    warnings = []
    vid, value_id = item["version_id"], item["value_id"]
    if decision == "approve":
        conn.execute("UPDATE field_values SET validation='human_approved' WHERE value_id=%s", (value_id,))
        corrected = None
    elif decision == "reject":
        conn.execute(
            """UPDATE field_values SET status='ambiguous', raw_value=NULL, normalized_value=NULL, quote=NULL,
                 source_sha256=NULL, page_no=NULL, display=%s, validation='human_approved',
                 note=concat_ws('; ', note, %s::text) WHERE value_id=%s""",
            (display_for("ambiguous", None), f"rejected in review: {comment}".strip(": "), value_id))
        corrected = {"status": "ambiguous"}
    elif decision == "correct":
        status = new["status"]
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        sha = page = quote = None
        if status in ("found", "stated_none"):
            files = {f["file_name"]: f for f in version_files(conn, vid)}
            f = files.get(new.get("source_pdf") or item["source_pdf"])
            if f is None:
                raise ValueError(f"source_pdf must be one of: {list(files)}")
            sha, page, quote = f["sha256"], int(new["page"]), new["quote"]
            if not quote:
                raise ValueError("a found / stated_none value needs a quote")
            pages = {p.page_no: p for p in read_pages(f["md_path"], "D1", sha, f["file_name"], "")}
            if page not in pages:
                warnings.append(f"page {page} has no text in the .md (OCR page?) - quote not checked")
            elif not quote_check(quote, pages[page].text, pages[page].ocr)[0]:
                warnings.append("the quote was not found on that page of the .md text")
        norm = new.get("normalized_value")
        if isinstance(norm, str) and norm.strip():
            norm = json.loads(norm)
        elif norm == "":
            norm = None
        raw = new.get("raw_value") or None
        conn.execute(
            """UPDATE field_values SET status=%s, raw_value=%s, normalized_value=%s::jsonb, quote=%s,
                 source_sha256=%s, page_no=%s, display=%s, validation='human_approved', confidence='high',
                 note=concat_ws('; ', note, %s::text) WHERE value_id=%s""",
            (status, raw, None if norm is None else json.dumps(norm), quote, sha, page,
             display_for(status, raw), f"corrected in review: {comment}".strip(": "), value_id))
        corrected = {"status": status, "raw_value": raw, "normalized_value": norm, "quote": quote,
                     "source_sha256": sha, "page": page}
    else:
        return ["skipped"]
    conn.execute("""UPDATE review_items SET decision=%s, reviewer=%s, corrected_value=%s::jsonb, decided_at=now()
                    WHERE review_id=%s""",
                 ({"approve": "approved", "correct": "corrected", "reject": "rejected"}[decision], reviewer,
                  None if corrected is None else json.dumps(corrected), item["review_id"]))
    conn.execute("""UPDATE conflicts SET resolved=true, resolution=%s
                    WHERE version_id=%s AND field_name=%s AND NOT resolved""",
                 (f"{decision} by {reviewer}" + (f": {comment}" if comment else ""), vid, item["field_name"]))
    finish_version(conn, vid)
    write_card(conn, cfg, item["card_id"], load_fields()["prompt_version"])
    conn.commit()
    return warnings


def _show(item):
    print("\n" + "=" * 78)
    print(f"[review {item['review_id']}] {item['issuer']} / {item['card_name']} (version {item['version_id']})")
    print(f"field:      {item['field_name']}")
    print(f"why:        {item['reason']}")
    print(f"status:     {item['status']}")
    print(f"value:      {item['raw_value']}")
    print(f"normalized: {json.dumps(item['normalized_value'])}")
    print(f"quote:      {item['quote']}")
    print(f"source:     {item['source_pdf']} page {item['page_no']}")


def interactive(cfg: Config, reviewer: str) -> dict:
    counts = {"approved": 0, "corrected": 0, "rejected": 0, "skipped": 0}
    with connect(cfg.database_url) as conn:
        items = open_items(conn, cfg.quarter)
        print(f"{len(items)} open review items for {cfg.quarter}")
        for item in items:
            _show(item)
            for f in version_files(conn, item["version_id"]):
                print(f"   file: {f['file_name']} ({f['page_count']} pages)")
            ans = input("[a]pprove  [c]orrect  [r]eject  [s]kip  [q]uit > ").strip().lower()[:1]
            if ans == "q":
                break
            decision = {"a": "approve", "c": "correct", "r": "reject"}.get(ans, "skip")
            new, comment = None, ""
            if decision == "correct":
                new = {"status": input(f"status {STATUSES} > ").strip() or item["status"],
                       "raw_value": input("raw value (as written) > ").strip(),
                       "normalized_value": input("normalized value (JSON, empty = null) > ").strip(),
                       "quote": input("quote (copied from the PDF) > ").strip(),
                       "source_pdf": input(f"source PDF [{item['source_pdf']}] > ").strip() or item["source_pdf"],
                       "page": input(f"page [{item['page_no']}] > ").strip() or item["page_no"]}
            if decision != "skip":
                comment = input("comment (optional) > ").strip()
            try:
                for w in apply_decision(conn, cfg, item, decision, reviewer, new, comment):
                    print("   note:", w)
                counts[{"approve": "approved", "correct": "corrected", "reject": "rejected",
                        "skip": "skipped"}[decision]] += 1
            except Exception as e:  # noqa: BLE001 - show the problem, keep the item open
                conn.rollback()
                print("   NOT SAVED:", e)
    print(counts)
    return counts


def export_csv(cfg: Config, path: str) -> int:
    with connect(cfg.database_url) as conn:
        items = open_items(conn, cfg.quarter)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, COLUMNS)
        w.writeheader()
        for it in items:
            w.writerow({"review_id": it["review_id"], "issuer": it["issuer"], "card": it["card_name"],
                        "version_id": it["version_id"], "field": it["field_name"], "reason": it["reason"],
                        "status": it["status"], "raw_value": it["raw_value"],
                        "normalized_value": json.dumps(it["normalized_value"]), "quote": it["quote"],
                        "source_pdf": it["source_pdf"], "page": it["page_no"]})
    print(f"{len(items)} open items written to {path}. Fill 'decision' (approve / correct / reject); "
          f"for correct also the new_* columns. Then: python run.py review --import {path}")
    return len(items)


def import_csv(cfg: Config, path: str, reviewer: str) -> dict:
    log = setup_logging(cfg, "review")
    counts = {"approved": 0, "corrected": 0, "rejected": 0, "skipped": 0, "errors": 0}
    with connect(cfg.database_url) as conn, open(path, encoding="utf-8-sig") as f:
        items = {it["review_id"]: it for it in open_items(conn, cfg.quarter)}
        for row in csv.DictReader(f):
            decision = (row.get("decision") or "").strip().lower()
            item = items.get(int(row["review_id"]))
            if item is None or decision not in ("approve", "correct", "reject"):
                counts["skipped"] += 1
                continue
            new = {"status": row.get("new_status") or item["status"], "raw_value": row.get("new_raw_value"),
                   "normalized_value": row.get("new_normalized_value") or "", "quote": row.get("new_quote"),
                   "source_pdf": row.get("new_source_pdf") or item["source_pdf"],
                   "page": row.get("new_page") or item["page_no"]}
            try:
                for w in apply_decision(conn, cfg, item, decision, reviewer, new, row.get("comment") or ""):
                    log.warning("review %s: %s", row["review_id"], w)
                counts[{"approve": "approved", "correct": "corrected", "reject": "rejected"}[decision]] += 1
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                counts["errors"] += 1
                log.error("review %s NOT SAVED: %s", row["review_id"], e)
    log.info("review import done: %s", counts)
    return counts


def run(cfg: Config, export=None, import_=None, reviewer=None):
    reviewer = reviewer or getpass.getuser()
    if export:
        return export_csv(cfg, export)
    if import_:
        return import_csv(cfg, import_, reviewer)
    return interactive(cfg, reviewer)
