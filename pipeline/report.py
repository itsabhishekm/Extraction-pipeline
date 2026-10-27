"""Audit outputs from Postgres (audit-v6 formats):

    python run.py report

  <data_root>/json/<bank_slug>/<bank_slug>__inventory.json    one per bank (Layers 1-3, 5, coverage,
                                                              eligibility, cardholder_request_note,
                                                              self_check, flags)
  <data_root>/reports/<quarter>_summary.md                    Output 2 field availability table,
                                                              Output 3 per-PDF approach table,
                                                              Output 4 short findings
The card files themselves are written by extract / validate / retry / review (json_out.py).
"""
import json
from collections import Counter, defaultdict
from datetime import date

from .config import Config, progress, setup_logging
from .db import connect
from .integrity import bank_self_check
from .stage_extract import fields_by_name, load_fields

DOC_TYPES = ["pricing_disclosure", "cardholder_agreement", "combined", "application_disclosure",
             "multi_card_disclosure", "rewards_terms", "privacy_notice", "other"]
FOUND = ("found", "stated_none")


def cardholder_note(issuer) -> str:
    name = issuer["issuer_name_in_pdf"] or issuer["folder_name"]
    contact = ", ".join([*(issuer["contact_phone"] or []), *([issuer["contact_website"]] if issuer["contact_website"] else [])])
    return (f"If your card is not listed here, ask {name} for your agreement"
            + (f" ({contact})" if contact else "")
            + ". Card issuers must provide it within 30 days of your request.")


def bank_inventory(conn, cfg, issuer, prompt_version, models) -> dict:
    iid = issuer["issuer_id"]
    files = conn.execute(
        """SELECT f.*, d.* FROM document_files f JOIN documents d USING (sha256)
           WHERE f.issuer_id=%s AND f.quarter=%s ORDER BY f.file_name""", (iid, cfg.quarter)).fetchall()
    first_name = {}
    for f in files:
        first_name.setdefault(f["sha256"], f["file_name"])
    cards = conn.execute(
        """SELECT c.card_id, c.card_name, c.card_slug, count(v.*) AS n,
                  max(v.version_date) FILTER (WHERE v.is_latest) AS latest_date, max(v.json_path) AS json_path
           FROM cards c JOIN card_versions v USING (card_id)
           WHERE c.issuer_id=%s AND v.quarter=%s GROUP BY 1, 2, 3 ORDER BY 2""", (iid, cfg.quarter)).fetchall()
    described = defaultdict(set)
    for r in conn.execute(
            """SELECT vd.sha256, c.card_name FROM version_documents vd JOIN card_versions v USING (version_id)
               JOIN cards c USING (card_id) WHERE c.issuer_id=%s AND v.quarter=%s""", (iid, cfg.quarter)).fetchall():
        described[r["sha256"]].add(r["card_name"])
    doc_reviews = defaultdict(list)
    for r in conn.execute(
            """SELECT vd.sha256, unnest(v.review_reasons) AS reason FROM version_documents vd
               JOIN card_versions v USING (version_id) JOIN cards c USING (card_id)
               WHERE c.issuer_id=%s AND v.quarter=%s""", (iid, cfg.quarter)).fetchall():
        doc_reviews[r["sha256"]].append(r["reason"])

    pdfs, type_counts = [], Counter()
    for f in files:
        dup = first_name[f["sha256"]] if first_name[f["sha256"]] != f["file_name"] else None
        if not dup:
            type_counts[f["document_type"] or "other"] += 1
        reasons = sorted(set(doc_reviews.get(f["sha256"], [])))
        if f["status"] == "failed":
            reasons.append(f"could not be read: {f['last_error']}")
        ocr = f["ocr_pages"] or []
        pdfs.append({
            "file_name": f["file_name"], "submission_id": f["submission_id"], "sha256": f["sha256"],
            "duplicate_of": dup, "document_type": f["document_type"],
            "cards_described": sorted(described.get(f["sha256"], [])), "read_method": f["text_source"],
            "metadata": {"file_size_kb": float(f["file_size_kb"] or 0), "page_count": f["page_count"],
                         "pdf_version": f["pdf_version"], "producer": f["producer"], "created": f["pdf_created"],
                         "modified": f["pdf_modified"], "encrypted": f["encrypted"], "languages": f["languages"] or []},
            "structure": {"content_type": f["content_type"],
                          "has_text_layer": (f["text_pages"] or 0) > 0,
                          "text_layer_complete": not ocr,
                          "text_layer_readable": f["status"] == "ok",
                          "ocr_pages": ocr, "layout_notes": f["layout_notes"] or [], "errors": f["errors"] or []},
            "extraction_approach": {"tag": f["extraction_tag"], "reason": f["extraction_tag_reason"]},
            "needs_manual_review": bool(reasons) or f["extraction_tag"] == "MANUAL_REVIEW",
            "review_reasons": reasons,
        })

    latest = conn.execute(
        """SELECT v.*, c.card_name FROM card_versions v JOIN cards c USING (card_id)
           WHERE c.issuer_id=%s AND v.quarter=%s AND v.is_latest""", (iid, cfg.quarter)).fetchall()
    coverage = {}
    for r in conn.execute(
            """SELECT fv.field_name, count(*) FILTER (WHERE fv.status IN ('found','stated_none')) AS w, count(*) AS t
               FROM field_values fv WHERE fv.version_id = ANY(%s) GROUP BY 1 ORDER BY 1""",
            ([v["version_id"] for v in latest],)).fetchall():
        coverage[r["field_name"]] = {"cards_with_field": r["w"], "cards_total": r["t"]}
    excluded = [{"card_name": v["card_name"], "reasons": v["eligibility_reasons"] or []}
                for v in latest if v["comparison_eligible"] is False]
    flags = []
    for v in latest:
        for reason in v["review_reasons"] or []:
            flags.append({"file_name": None, "page": None, "issue": f"{v['card_name']}: {reason}",
                          "recommended_action": "python run.py review" if "human_review" in reason
                          else "check the PDF and correct in review if needed"})
    for f in pdfs:
        if f["structure"]["errors"] or f["metadata"]["page_count"] is None:
            flags.append({"file_name": f["file_name"], "page": None, "issue": "; ".join(f["structure"]["errors"])
                          or "could not be read", "recommended_action": "open the file manually"})
    fails = bank_self_check(conn, cfg, iid)
    return {
        "prompt_version": "audit-v6", "pipeline_prompt_version": prompt_version,
        "audited_by": "pipeline", "audited_on": date.today().isoformat(), "model": models,
        "bank_name": issuer["bank_slug"], "bank_folder": issuer["folder_name"],
        "issuer_name_in_pdf": issuer["issuer_name_in_pdf"] or "", "charter_hint": issuer["charter_hint"] or "",
        "pdf_count": len(files), "unique_pdf_count": len(first_name),
        "document_type_counts": {t: type_counts.get(t, 0) for t in DOC_TYPES},
        "cards": [{"card_name": c["card_name"], "card_file": f"{issuer['bank_slug']}__{c['card_slug']}.json",
                   "version_count": c["n"], "latest_version_date": str(c["latest_date"] or "")} for c in cards],
        "pdfs": pdfs,
        "field_coverage": coverage,
        "benchmark_eligibility": {"eligible_cards": sum(1 for v in latest if v["comparison_eligible"]),
                                  "excluded_cards": len(excluded), "excluded": excluded},
        "cardholder_request_note": cardholder_note(issuer),
        "self_check": "passed" if not fails else fails,
        "flags": flags,
    }


def summary(conn, cfg, spec, inventories) -> str:
    fdefs = fields_by_name(spec)
    issuers_total = len(inventories)
    rows = conn.execute(
        """SELECT fv.field_name, fv.status, fv.confidence, fv.validation, fv.attempts, fv.approach,
                  fv.validation_errors, c.issuer_id
           FROM field_values fv JOIN card_versions v USING (version_id) JOIN cards c USING (card_id)
           WHERE v.quarter=%s AND v.is_latest""", (cfg.quarter,)).fetchall()
    by_field = defaultdict(list)
    for r in rows:
        by_field[r["field_name"]].append(r)
    cards_total = conn.execute(
        "SELECT count(*) AS n FROM card_versions WHERE quarter=%s AND is_latest", (cfg.quarter,)).fetchone()["n"]
    out = [f"# Batch summary - {cfg.quarter}", "", f"{issuers_total} issuers, {cards_total} cards (latest versions).", "",
           "## Output 2: Field availability", "",
           "| Target field | In the PDFs? (issuers found / total, %) | Cards found / total | How reliable | "
           "Watch out for | Recommended approach tag |", "|---|---|---|---|---|---|"]
    weak, missing = [], []
    for fname in list(fdefs) + ["submission_id"]:
        rs = by_field.get(fname, [])
        if not rs:
            continue
        iss_found = len({r["issuer_id"] for r in rs if r["status"] in FOUND})
        cards_found = sum(r["status"] in FOUND for r in rs)
        found_rows = [r for r in rs if r["status"] in FOUND]
        first_pass = sum(r["validation"] == "passed" and r["attempts"] == 1 for r in found_rows)
        high = sum(r["confidence"] == "high" for r in found_rows)
        if found_rows:
            fp = first_pass / len(found_rows)
            level = "high" if fp >= 0.9 else "medium" if fp >= 0.7 else "low"
            why = f"{fp:.0%} passed validation first time, {high / len(found_rows):.0%} high confidence"
        else:
            level, why = "-", "never found"
        errs = Counter(e.split(":")[0] for r in rs for e in (r["validation_errors"] or []))
        watch = ", ".join(f"{k} ({n})" for k, n in errs.most_common(2)) or "-"
        tag = Counter(r["approach"] for r in found_rows if r["approach"]).most_common(1)
        pct = iss_found / issuers_total if issuers_total else 0
        out.append(f"| {fname} | {iss_found} / {issuers_total} ({pct:.0%}) | {cards_found} / {len(rs)} | "
                   f"{level}: {why} | {watch} | {tag[0][0] if tag else '-'} |")
        if pct < 0.5:
            missing.append(f"{fname} ({pct:.0%} of issuers)")
        if level == "low":
            weak.append(f"{fname} ({why})")

    out += ["", "## Output 3: Per-issuer / per-PDF approach", "",
            "| Issuer | PDF | Document type | Content type | Approach tag | Manual review? | Reason |",
            "|---|---|---|---|---|---|---|"]
    structures, review_groups = Counter(), defaultdict(list)
    for inv in inventories:
        uniq = [p for p in inv["pdfs"] if not p["duplicate_of"]]
        for p in uniq:
            dups = [d["file_name"] for d in inv["pdfs"] if d["duplicate_of"] == p["file_name"]]
            structures[p["structure"]["content_type"] or "unreadable"] += 1
            name = p["file_name"] + (f" (duplicates: {', '.join(dups)})" if dups else "")
            out.append(f"| {inv['bank_folder']} | {name} | {p['document_type']} | {p['structure']['content_type']} | "
                       f"{p['extraction_approach']['tag']} | {'yes' if p['needs_manual_review'] else 'no'} | "
                       f"{p['extraction_approach']['reason'] or ''} |")
            for reason in p["review_reasons"]:
                review_groups[reason.split(":")[-1].strip() if ": " in reason else reason].append(
                    f"{inv['bank_folder']} / {p['file_name']}")

    excl = Counter(reason for inv in inventories for e in inv["benchmark_eligibility"]["excluded"] for reason in e["reasons"])
    gates = conn.execute(
        """SELECT r AS reason, count(*) AS n FROM card_versions v, unnest(v.review_reasons) r
           WHERE v.quarter=%s AND (r LIKE 'quality gate:%%' OR r LIKE 'daily periodic rate%%') GROUP BY 1 ORDER BY 2 DESC""",
        (cfg.quarter,)).fetchall()
    conflicts = conn.execute("SELECT count(*) AS n FROM conflicts c JOIN card_versions v USING (version_id) "
                             "WHERE v.quarter=%s", (cfg.quarter,)).fetchone()["n"]
    out += ["", "## Output 4: Short findings", "",
            "- Most common PDF structures: " + ", ".join(f"{k} ({n})" for k, n in structures.most_common()),
            "- Fields frequently missing (found for under half of the issuers): " + (", ".join(missing) or "none"),
            "- Fields that are unreliable (under 70% pass validation first time): " + (", ".join(weak) or "none"),
            f"- Conflicts between PDFs of the same version: {conflicts}",
            "- PDFs needing manual review, by reason:"]
    for reason, items in sorted(review_groups.items(), key=lambda x: -len(x[1])):
        out.append(f"  - {reason} ({len(items)}): " + "; ".join(sorted(set(items))[:10]))
    out.append("- Cards excluded from comparison and benchmarking, by reason:")
    out += [f"  - {k}: {n}" for k, n in excl.most_common()] or ["  - none"]
    out.append("- Quality-gate flags raised:")
    out += [f"  - {g['reason']} ({g['n']})" for g in gates] or ["  - none"]
    failed_checks = [inv["bank_folder"] for inv in inventories if inv["self_check"] != "passed"]
    out.append(f"- Banks whose self-check did not pass: {len(failed_checks)}"
               + (": " + ", ".join(failed_checks) if failed_checks else ""))
    return "\n".join(out) + "\n"


def run(cfg: Config) -> dict:
    log = setup_logging(cfg, "report")
    spec = load_fields()
    models = {k: v for k, v in cfg.section("models").items() if k in ("ocr", "extract")}
    inventories = []
    with connect(cfg.database_url) as conn:
        issuers = conn.execute(
            """SELECT DISTINCT i.* FROM issuers i JOIN document_files f USING (issuer_id)
               WHERE f.quarter=%s ORDER BY i.folder_name""", (cfg.quarter,)).fetchall()
        progress.start(len(issuers), "banks")
        for ii, iss in enumerate(issuers, start=1):
            progress.step(ii, iss["folder_name"][:30])
            inv = bank_inventory(conn, cfg, iss, spec["prompt_version"], models)
            path = cfg.data_root / "json" / iss["bank_slug"] / f"{iss['bank_slug']}__inventory.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(inv, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            inventories.append(inv)
        progress.stop()
        md = summary(conn, cfg, spec, inventories)
    out = cfg.data_root / "reports" / f"{cfg.quarter}_summary.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    passed = sum(inv["self_check"] == "passed" for inv in inventories)
    log.info("report: %d inventory files (%d self-check passed), summary %s", len(inventories), passed, out)
    return {"inventories": len(inventories), "self_check_passed": passed, "summary": str(out)}
