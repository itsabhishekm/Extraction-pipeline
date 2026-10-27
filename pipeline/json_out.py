"""Write one JSON file per card, in the audit-v6 card-file format, from the database.

    <data_root>/json/<bank_slug>/<bank_slug>__<card_slug>.json

All versions of a card go in one file (newest first). Versions without extracted fields
are listed with audit_level "inventory_only".
"""
import json
from pathlib import Path

from .postprocess import DATA_SCOPE

STATUS_ORDER = ["found", "stated_none", "not_applicable", "not_present", "unreadable", "ambiguous"]


def write_card(conn, cfg, card_id: int, prompt_version: str) -> Path:
    card = conn.execute(
        """SELECT c.*, i.bank_slug, i.folder_name FROM cards c JOIN issuers i USING (issuer_id)
           WHERE c.card_id = %s""", (card_id,)).fetchone()
    versions = conn.execute(
        """SELECT * FROM card_versions WHERE card_id = %s
           ORDER BY version_date DESC NULLS LAST, version_id DESC""", (card_id,)).fetchall()
    latest = next((v for v in versions if v["is_latest"]), versions[0] if versions else None)

    out_versions, conflicts = [], []
    for v in versions:
        docs = conn.execute(
            """SELECT vd.sha256, vd.role, f.file_name, f.submission_id
               FROM version_documents vd
               JOIN LATERAL (SELECT file_name, submission_id FROM document_files x
                             WHERE x.sha256 = vd.sha256 AND x.quarter = %s ORDER BY file_id LIMIT 1) f ON true
               WHERE vd.version_id = %s ORDER BY vd.role, f.file_name""", (v["quarter"], v["version_id"])).fetchall()
        rows = conn.execute(
            """SELECT fv.*, f.file_name AS source_pdf, r.extraction_model, r.ocr_model, r.prompt_version AS run_prompt,
                      r.code_version, r.started_at AS run_started
               FROM field_values fv
               LEFT JOIN pipeline_runs r ON r.run_id = fv.run_id
               LEFT JOIN LATERAL (SELECT file_name FROM document_files x WHERE x.sha256 = fv.source_sha256
                                  ORDER BY file_id LIMIT 1) f ON true
               WHERE fv.version_id = %s ORDER BY fv.field_name""", (v["version_id"],)).fetchall()
        regs = conn.execute(
            """SELECT m.reg_code, m.mention_type, m.state, m.quote, m.page_no, m.confidence, m.validation,
                      f.file_name AS source_pdf
               FROM regulation_mentions m
               LEFT JOIN LATERAL (SELECT file_name FROM document_files x WHERE x.sha256 = m.source_sha256
                                  ORDER BY file_id LIMIT 1) f ON true
               WHERE m.version_id = %s ORDER BY m.reg_code""", (v["version_id"],)).fetchall()
        fields, coverage = {}, {s: [] for s in STATUS_ORDER}
        for r in rows:
            fields[r["field_name"]] = {
                "raw_value": r["raw_value"], "normalized_value": r["normalized_value"], "quote": r["quote"] or "",
                "source_pdf": r["source_pdf"], "page": r["page_no"], "location_type": r["location_type"],
                "status": r["status"], "display": r["display"], "confidence": r["confidence"],
                "approach_tag": r["approach"], "note": r["note"] or "",
                "validation": r["validation"], "validation_errors": r["validation_errors"] or [],
                "lineage": {"source_sha256": r["source_sha256"], "run_id": r["run_id"], "attempts": r["attempts"],
                            "model": r["extraction_model"], "ocr_model": r["ocr_model"],
                            "prompt_version": r["run_prompt"], "code_version": r["code_version"]},
            }
            coverage[r["status"]].append(r["field_name"])
        for c in conn.execute(
                """SELECT c.*, fa.file_name AS pdf_a, fb.file_name AS pdf_b FROM conflicts c
                   LEFT JOIN LATERAL (SELECT file_name FROM document_files x WHERE x.sha256 = c.sha256_a
                                      ORDER BY file_id LIMIT 1) fa ON true
                   LEFT JOIN LATERAL (SELECT file_name FROM document_files x WHERE x.sha256 = c.sha256_b
                                      ORDER BY file_id LIMIT 1) fb ON true
                   WHERE c.version_id = %s ORDER BY c.conflict_id""", (v["version_id"],)).fetchall():
            conflicts.append({"version_date": v["version_date"].isoformat() if v["version_date"] else None,
                              "field": c["field_name"],
                              "a": {"value": c["value_a"], "source_pdf": c["pdf_a"], "page": c["page_a"]},
                              "b": {"value": c["value_b"], "source_pdf": c["pdf_b"], "page": c["page_b"]},
                              "resolved": c["resolved"], "resolution": c["resolution"]})
        out_versions.append({
            "version_date": v["version_date"].isoformat() if v["version_date"] else None,
            "version_date_source": v["version_date_source"],
            "submission_ids": sorted({d["submission_id"] for d in docs if d["submission_id"]}),
            "effective_date": v["effective_date"].isoformat() if v["effective_date"] else None,
            "is_latest": v["is_latest"],
            "audit_level": "full" if rows else "inventory_only",
            "source_pdfs": [{"file_name": d["file_name"], "role": d["role"], "sha256": d["sha256"]} for d in docs],
            "fields": fields,
            "regulations_mentioned": [dict(r) for r in regs],
            "coverage": coverage if rows else {s: [] for s in STATUS_ORDER},
            "needs_manual_review": v["needs_manual_review"],
            "review_reasons": v["review_reasons"] or [],
            "pipeline_stage": v["stage"],
            "product_name": v["product_name"],
            "products_in_same_pdfs": v["products_in_group"] or [],
        })

    data = {
        "prompt_version": prompt_version,
        "bank_name": card["bank_slug"],
        "bank_folder": card["folder_name"],
        "card_name": card["card_name"],
        "card_name_source": card["card_name_source"],
        "card_type": latest["card_type"] if latest else None,
        "market_scope": latest["market_scope"] if latest else None,
        "secured": latest["secured"] if latest else None,
        "data_scope": DATA_SCOPE,
        "terms_as_of": latest["terms_as_of"] if latest else None,
        "comparison_eligible": latest["comparison_eligible"] if latest else None,
        "eligibility_reasons": (latest["eligibility_reasons"] or []) if latest else [],
        "versions": out_versions,
        "conflicts": conflicts,
    }
    out = cfg.data_root / "json" / card["bank_slug"] / f"{card['bank_slug']}__{card['card_slug']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    conn.execute("UPDATE card_versions SET json_path = %s WHERE card_id = %s", (str(out), card_id))
    return out
