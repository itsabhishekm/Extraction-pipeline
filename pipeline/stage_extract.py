"""Stage 5 - extract: fill the fields of every card version with Qwen3.5-4B.

For each version (stage 'grouped'):
  1. read the .md pages of its PDFs (D1, D2, ... in the prompt)
  2. list the card PRODUCTS the PDFs cover. More than one -> multi-card: the version is split
     into one version per product (all sharing the same PDFs) and each is extracted for its
     own product only
  3. for each field group in fields.yaml: pick the pages that match the group's keywords and
     ask the model for JSON in a fixed schema (thinking on only where fields.yaml says so;
     worked examples from examples.yaml when present)
  4. apply the status / normalization rules in code (postprocess.py)
  5. find regulation mentions (keyword search, then the model confirms and classifies)
  6. save field_values + regulation_mentions, name the card after the product printed in the
     PDF, set version date, eligibility and latest version, write the card JSON file
The version moves to stage 'extracted'; the validate stage checks every value next.
"""
import json
import re
from pathlib import Path

import pymupdf
import yaml

from . import llm
from .config import HERE, Config, progress, setup_logging
from .db import connect, finish_run, start_run
from .json_out import write_card
from .naming import slugify
from .pages import read_pages, render, select_pages
from .postprocess import (STATUSES, clean_field, display_for, date_from_file_name, eligibility, parse_date,
                          pdf_created_date, terms_as_of)

SYSTEM = """You extract facts from U.S. credit card agreements submitted to the CFPB.
Answer ONLY from the text provided. Never guess or use outside knowledge.

The text is split into pages. Each page starts with a header like === [D1 page 3] ===.
For every field return:
- status: found (the value is printed) | stated_none (the text says "None", "$0" or equivalent) |
  not_applicable (the field cannot exist for this card) | not_present (the text does not mention it) |
  unreadable (text is garbled) | ambiguous (mentioned but the meaning is unclear)
- raw_value: the value exactly as written, or null
- normalized_value: the value in the shape given for that field, or null
- quote: the exact sentence or table cells the value comes from, copied character for character
  from the text (no markdown symbols like ** or |), or null
- doc and page: from the header of the page the quote comes from (e.g. doc "D1", page 3), or null
- confidence: high | medium | low

"Not mentioned" is not_present, never "none". Percentages are plain numbers (18.49, not "18.49%").
Amounts are numbers in USD (41, not "$41")."""

PRODUCTS_TASK = """List the distinct credit card PRODUCTS these documents cover (e.g. "Platinum Visa",
"Secured Visa", "Cash Rewards Mastercard"), with the exact product name as printed.
Rules:
- Rate, credit-limit or creditworthiness tiers of one product are ONE product.
- A secured and an unsecured product are DIFFERENT products.
- The same product offered as Visa and Mastercard with the same terms is ONE product.
- Programs, plans, benefits or rewards that are not separate cards are not products.
- If the documents cover only one product, return just that one.
For each product give a quote that names it, with doc and page."""


def load_fields() -> dict:
    return yaml.safe_load((HERE / "fields.yaml").read_text(encoding="utf-8"))


def load_examples() -> dict:
    p = HERE / "examples.yaml"
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("groups") or {}


def require_all(schema: dict) -> dict:
    """Copy of a normalized-value schema with every object key required (values may still be null).
    Ollama turns the schema into a grammar; optional keys are simply left out by a small model
    (e.g. an APR answer with no "min"/"max"), so every key must be written, as null if unknown."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: require_all(v) for k, v in out["properties"].items()}
        out["required"] = list(out["properties"])
    if isinstance(out.get("items"), dict):
        out["items"] = require_all(out["items"])
    return out


def nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]} if schema.get("type") != "null" else {"type": "null"}


def field_schema(norm: dict, doc_labels: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": STATUSES},
            "raw_value": {"type": ["string", "null"]},
            "normalized_value": nullable(require_all(norm)),
            "quote": {"type": ["string", "null"]},
            "doc": {"type": ["string", "null"], "enum": doc_labels + [None]},
            "page": {"type": ["integer", "null"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["status", "raw_value", "normalized_value", "quote", "doc", "page", "confidence"],
    }


def group_schema(fields: dict, doc_labels: list[str]) -> dict:
    return {"type": "object",
            "properties": {name: field_schema(f["normalized"], doc_labels) for name, f in fields.items()},
            "required": list(fields)}


def field_instructions(fields: dict) -> str:
    lines = []
    for i, (name, f) in enumerate(fields.items(), start=1):
        lines.append(f"{i}. {name}: {f['question']}")
        for r in f.get("rules") or []:
            lines.append(f"   - {r}")
        norm = f["normalized"]
        if norm.get("type") != "null":
            lines.append(f"   normalized_value shape: {yaml.safe_dump(norm, default_flow_style=True, width=1000).strip()}")
    return "\n".join(lines)


def docs_header(docs: list[dict]) -> str:
    return "\n".join(f"{d['label']} = {d['file_name']} ({d['role']}, {d['page_count']} pages)" for d in docs)


def product_note(product: str | None, products: list[str]) -> str:
    if not product:
        return ""
    return (f"\nTHIS CARD VERSION IS FOR ONE PRODUCT: {product}\n"
            f"The documents cover {len(products)} products: {', '.join(products)}.\n"
            f"Extract values for {product} ONLY. A value printed once for all products applies to {product} too. "
            f"If the text does not let you tell {product}'s value apart from the others, use status ambiguous.\n")


def examples_text(examples: dict, group: str, fields: dict, limit: int = 2) -> str:
    out = []
    for ex in (examples.get(group) or [])[:limit]:
        answer = {k: v for k, v in (ex.get("answer") or {}).items() if k in fields}
        if not answer:
            continue
        # example pages are labelled "EX1", "EX2" so they can never be mistaken for this version's D1, D2
        text = re.sub(r"=== \[D(\d+) page", r"=== [EX\1 page", ex["text"])
        answer = {k: {**v, "doc": re.sub(r"^D(\d+)$", r"EX\1", v["doc"]) if v.get("doc") else v.get("doc")}
                  for k, v in answer.items()}
        out.append(f"--- worked example (a different card; never quote it) ---\nEXAMPLE TEXT:\n{text}\n"
                   f"EXAMPLE ANSWER:\n{json.dumps(answer, ensure_ascii=False)}")
    return ("\nWORKED EXAMPLES (show the expected answer format; do not copy their values):\n"
            + "\n\n".join(out) + "\n") if out else ""


def page_images(cfg: Config, docs: list[dict], pages, max_pages: int = 3, dpi: int = 110) -> list[bytes]:
    """Render the given pages to PNG for the vision fallback."""
    paths = {d["sha256"]: cfg.input_root / d["folder_name"] / d["file_name"] for d in docs}
    out, opened = [], {}
    for p in pages[:max_pages]:
        if p.sha256 not in opened:
            opened[p.sha256] = pymupdf.open(paths[p.sha256], filetype="pdf")
        out.append(opened[p.sha256][p.page_no - 1].get_pixmap(dpi=dpi).tobytes("png"))
    return out


def extract_group(model, gdef, fields, docs, pages, *, group="", think=None, max_chars=None, feedback="",
                  product=None, products=(), examples=None, cfg=None, use_images=False, meta=None):
    """One model call for `fields` (a subset of a group's fields). Returns (answers, stats)."""
    max_chars = max_chars or gdef.get("max_chars", 14000)
    keywords = list(gdef.get("keywords", [])) + ([product.lower()] if product else [])
    chosen = select_pages(pages, keywords, max_chars, gdef.get("first_pages", 0), gdef.get("last_pages", 0),
                          prefer_roles=("pricing", "combined") if group == "pricing" else (),
                          anchors=tuple(gdef.get("anchors", [])))
    if not chosen:
        # no page mentions anything this group looks for: nothing to ask the model
        return ({f: {"status": "not_present", "raw_value": None, "normalized_value": None, "quote": None,
                     "doc": None, "page": None, "confidence": "medium"} for f in fields},
                {"seconds": 0.0, "pages_sent": [], "skipped": True})
    labels = [d["label"] for d in docs]
    images = page_images(cfg, docs, chosen) if use_images and cfg else None
    user = (f"DOCUMENTS (one card version):\n{docs_header(docs)}\n"
            + product_note(product, list(products))
            + f"\nFIELDS TO EXTRACT:\n{field_instructions(fields)}\n"
            + examples_text(examples or {}, group, fields)
            + (f"\nYOUR PREVIOUS ANSWER WAS REJECTED:\n{feedback}\nFix it using the text below.\n" if feedback else "")
            + ("\nThe images are the same pages as the text, in the same order; use them to read tables whose "
               "columns are jumbled in the text. Quotes must still be copied from the text.\n" if images else "")
            + f"\nTEXT:\n{render(chosen, max_chars)}")
    data, stats = llm.extract_json(model, SYSTEM, user, group_schema(fields, labels),
                                   think=gdef.get("thinking", False) if think is None else think,
                                   images=images, kind=f"extract:{group}", meta=meta)
    stats["pages_sent"] = [f"{p.doc}p{p.page_no}" for p in chosen]
    return data, stats


def list_products(model, docs, pages, meta=None) -> list[str]:
    chosen = select_pages(pages, ["annual percentage rate", "card", "visa", "mastercard", "secured", "account",
                                  "interest rates and interest charges"], 12000, first_pages=2,
                          anchors=("interest rates and interest charges", "(apr) for purchases", "apr for purchases"))
    labels = [d["label"] for d in docs]
    schema = {"type": "object", "required": ["products"], "properties": {"products": {"type": "array", "maxItems": 25, "items": {
        "type": "object", "required": ["name", "quote", "doc", "page"],
        "properties": {"name": {"type": "string"}, "quote": {"type": "string"},
                       "doc": {"type": "string", "enum": labels}, "page": {"type": "integer"}}}}}}
    user = f"DOCUMENTS:\n{docs_header(docs)}\n\nTASK:\n{PRODUCTS_TASK}\n\nTEXT:\n{render(chosen, 12000)}"
    data, _ = llm.extract_json(model, SYSTEM, user, schema, think=False, kind="products", meta=meta)
    names, seen = [], set()
    for p in data.get("products", [])[:25]:
        name = (p.get("name") or "").strip()
        key = slugify(name)
        if name and key and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def extract_regulations(conn, model, rdef, docs, pages, meta=None):
    regs = conn.execute("SELECT reg_code, search_terms FROM regulations").fetchall()
    terms = [t for r in regs for t in (r["search_terms"] or [])]
    chosen = select_pages(pages, terms, rdef.get("max_chars", 14000))
    if not chosen:
        return [], {"pages_sent": []}
    codes = [r["reg_code"] for r in regs]
    labels = [d["label"] for d in docs]
    schema = {"type": "object", "required": ["mentions"], "properties": {"mentions": {"type": "array", "maxItems": 30, "items": {
        "type": "object",
        "required": ["reg_code", "mention_type", "state", "quote", "doc", "page", "confidence"],
        "properties": {
            "reg_code": {"type": "string", "enum": codes},
            "mention_type": {"type": "string", "enum": ["cited_by_name", "notice_included", "right_granted",
                                                        "exception", "indirect_reference"]},
            "state": {"type": ["string", "null"]},
            "quote": {"type": "string", "maxLength": 600},
            "doc": {"type": "string", "enum": labels},
            "page": {"type": "integer"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}}}}
    user = (f"DOCUMENTS:\n{docs_header(docs)}\n\nTASK:\n{rdef['question']}\n\n"
            f"TEXT:\n{render(chosen, rdef.get('max_chars', 14000))}")
    data, stats = llm.extract_json(model, SYSTEM, user, schema, think=rdef.get("thinking", False),
                                   kind="regulations", meta=meta)
    stats["pages_sent"] = [f"{p.doc}p{p.page_no}" for p in chosen]
    out, seen = [], set()
    for m in data.get("mentions", []):
        key = (m.get("reg_code"), m.get("mention_type"), (m.get("state") or "")[:2].upper() or None)
        if m.get("reg_code") not in codes or not m.get("quote") or key in seen:
            continue
        seen.add(key)
        out.append({**m, "state": key[2]})
    return out, stats


def version_context(conn, version_id: int):
    docs = conn.execute(
        """SELECT vd.sha256, vd.role, d.md_path, d.page_count, d.pdf_created, d.text_source, f.file_name,
                  i.folder_name
           FROM version_documents vd
           JOIN documents d ON d.sha256 = vd.sha256
           JOIN LATERAL (SELECT file_name, issuer_id FROM document_files x WHERE x.sha256 = vd.sha256
                         ORDER BY file_id LIMIT 1) f ON true
           JOIN issuers i ON i.issuer_id = f.issuer_id
           WHERE vd.version_id = %s
           ORDER BY CASE vd.role WHEN 'pricing' THEN 0 WHEN 'combined' THEN 1 WHEN 'agreement' THEN 2 ELSE 3 END,
                    f.file_name""", (version_id,)).fetchall()
    pages = []
    for i, d in enumerate(docs, start=1):
        d["label"] = f"D{i}"
        pages += read_pages(d["md_path"], d["label"], d["sha256"], d["file_name"], d["role"])
    return docs, pages


def split_multi_card(conn, v: dict, products: list[str], log) -> list[dict]:
    """Turn one version into one version per product. Returns the new sibling versions."""
    conn.execute("UPDATE card_versions SET product_name=%s, products_in_group=%s WHERE version_id=%s",
                 (products[0], products, v["version_id"]))
    v["product_name"], v["products_in_group"] = products[0], products
    siblings = []
    issuer_id = conn.execute("SELECT issuer_id FROM cards WHERE card_id=%s", (v["card_id"],)).fetchone()["issuer_id"]
    for name in products[1:]:
        slug = slugify(name)[:60].rstrip("_") or "card"
        card_id = conn.execute(
            """INSERT INTO cards (issuer_id, card_name, card_slug, card_name_source) VALUES (%s,%s,%s,'pdf')
               ON CONFLICT (issuer_id, card_slug) DO UPDATE SET card_slug = EXCLUDED.card_slug
               RETURNING card_id""", (issuer_id, name, slug)).fetchone()["card_id"]
        key = f"{v['group_key']}#{slug}"
        row = conn.execute(
            """INSERT INTO card_versions (card_id, quarter, group_key, stage, product_name, products_in_group)
               VALUES (%s,%s,%s,'grouped',%s,%s)
               ON CONFLICT (quarter, group_key) DO UPDATE SET product_name = EXCLUDED.product_name
               RETURNING *""", (card_id, v["quarter"], key, name, products)).fetchone()
        conn.execute("""INSERT INTO version_documents (version_id, sha256, role)
                        SELECT %s, sha256, role FROM version_documents WHERE version_id=%s ON CONFLICT DO NOTHING""",
                     (row["version_id"], v["version_id"]))
        siblings.append(row)
    conn.execute("""UPDATE documents SET document_type='multi_card_disclosure'
                    WHERE sha256 IN (SELECT sha256 FROM version_documents WHERE version_id=%s
                                     AND role IN ('pricing','combined','other'))""", (v["version_id"],))
    log.info("covers %d products -> split into one version each: %s", len(products), "; ".join(products))
    return siblings


def save_version(conn, cfg, v: dict, docs, pages, fields_def, results, regs, run_id, prompt_version):
    vid = v["version_id"]
    doc_map = {d["label"]: (d["sha256"], d["file_name"]) for d in docs}
    ocr_pages = {(p.sha256, p.page_no) for p in pages if p.ocr}
    conn.execute("DELETE FROM review_items WHERE value_id IN (SELECT value_id FROM field_values WHERE version_id=%s)",
                 (vid,))
    conn.execute("DELETE FROM field_values WHERE version_id = %s", (vid,))
    conn.execute("DELETE FROM regulation_mentions WHERE version_id = %s", (vid,))
    conn.execute("DELETE FROM conflicts WHERE version_id = %s", (vid,))

    for name, r in results.items():
        group = fields_def[name]["_group"]
        on_ocr = (r["source_sha256"], r["page"]) in ocr_pages
        loc = "image" if on_ocr else ("table" if group == "pricing" else "text")
        tag = "OCR" if on_ocr else ("TABLE_EXTRACT" if group == "pricing" else "AI_ASSISTED")
        has_evidence = r["status"] in ("found", "stated_none") or r["quote"]
        conn.execute(
            """INSERT INTO field_values (version_id, field_name, status, raw_value, normalized_value, quote,
                 source_sha256, page_no, location_type, display, confidence, approach, note, run_id)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (vid, name, r["status"], r["raw_value"], _json(r["normalized_value"]), r["quote"],
             r["source_sha256"] if has_evidence else None, r["page"] if has_evidence else None,
             loc, r["display"], r["confidence"], tag, r["note"] or None, run_id))

    # submission_id comes from the file names, not the model
    subs = conn.execute(
        """SELECT DISTINCT f.submission_id, f.file_name, f.sha256 FROM document_files f
           JOIN version_documents vd ON vd.sha256 = f.sha256
           WHERE vd.version_id = %s AND f.quarter = %s AND f.submission_id IS NOT NULL
           ORDER BY 1""", (vid, v["quarter"])).fetchall()
    if subs:
        ids = sorted({s["submission_id"] for s in subs})
        conn.execute(
            """INSERT INTO field_values (version_id, field_name, status, raw_value, normalized_value, quote,
                 source_sha256, page_no, location_type, display, confidence, approach, note, validation, run_id)
               VALUES (%s,'submission_id','found',%s,NULL,%s,%s,1,'text',%s,'high','TEXT_EXTRACT',
                       'from the file name','passed',%s)""",
            (vid, ", ".join(ids), subs[0]["file_name"], subs[0]["sha256"], ", ".join(ids), run_id))
    else:
        conn.execute(
            """INSERT INTO field_values (version_id, field_name, status, display, confidence, approach, note,
                 validation, run_id)
               VALUES (%s,'submission_id','not_present',%s,'high','TEXT_EXTRACT','no submission id in the file names',
                       'passed',%s)""", (vid, display_for("not_present", None), run_id))

    for m in regs:
        sha, _ = doc_map.get(m["doc"], (None, None))
        if not sha:
            continue
        conn.execute(
            """INSERT INTO regulation_mentions (version_id, reg_code, mention_type, state, quote, source_sha256,
                 page_no, confidence, run_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (vid, m["reg_code"], m["mention_type"], m["state"], m["quote"], sha, m["page"], m["confidence"], run_id))

    # issuer details (first value found wins)
    get = lambda n: results.get(n, {})  # noqa: E731
    contact = get("issuer_contact").get("normalized_value") or {}
    conn.execute(
        """UPDATE issuers SET
             issuer_name_in_pdf = COALESCE(issuer_name_in_pdf, %s),
             charter_hint = COALESCE(charter_hint, %s),
             contact_phone = COALESCE(contact_phone, %s),
             contact_website = COALESCE(contact_website, %s),
             contact_address = COALESCE(contact_address, %s)
           WHERE issuer_id = (SELECT issuer_id FROM cards WHERE card_id = %s)""",
        (_found(get("issuer_name_in_pdf")), _found(get("charter_hint")), contact.get("phone") or None,
         contact.get("website"), contact.get("address"), v["card_id"]))

    # card: the product this version is about (multi-card), else the product name printed in the PDF
    old_card = v["card_id"]
    card_id = old_card
    name = v.get("product_name") or _found(get("card_name"))
    if name:
        slug = slugify(name)[:60].rstrip("_") or "card"
        card_id = conn.execute(
            """INSERT INTO cards (issuer_id, card_name, card_slug, card_name_source)
               SELECT issuer_id, %s, %s, 'pdf' FROM cards WHERE card_id = %s
               ON CONFLICT (issuer_id, card_slug) DO UPDATE SET card_name = EXCLUDED.card_name,
                                                              card_name_source = 'pdf'
               RETURNING card_id""", (name, slug, old_card)).fetchone()["card_id"]

    # version date: printed effective date, else a date in a file name, else the PDF creation date
    eff = parse_date(get("effective_date").get("normalized_value")) if _found(get("effective_date")) else None
    vdate, vsrc = eff, "effective_date" if eff else None
    if not vdate:
        for d in docs:
            fd = date_from_file_name(d["file_name"])
            if fd:
                vdate, vsrc = fd, "file_name"
                break
    if not vdate:
        for d in docs:
            pd = pdf_created_date(d["pdf_created"])
            if pd:
                vdate, vsrc = pd, "pdf_created"
                break

    ok, reasons = eligibility(results)
    review = [f"{n}: {r['status']}" for n, r in results.items() if r["status"] in ("unreadable", "ambiguous")]
    review += [f"{n}: low confidence" for n, r in results.items() if r["confidence"] == "low"
               and r["status"] in ("found", "stated_none")]
    ms = get("market_scope").get("normalized_value")
    if ms in ("non_us", "unclear"):
        review.append(f"market_scope {ms}")
    conn.execute(
        """UPDATE card_versions SET card_id=%s, effective_date=%s, version_date=%s, version_date_source=%s,
             card_type=%s, market_scope=%s, secured=%s, comparison_eligible=%s, eligibility_reasons=%s,
             terms_as_of=%s, needs_manual_review=%s, review_reasons=%s, stage='extracted', is_latest=false
           WHERE version_id=%s""",
        (card_id, eff, vdate, vsrc, get("card_type").get("normalized_value"), ms,
         get("secured").get("normalized_value") if get("secured").get("status") in ("found", "stated_none") else None,
         ok, reasons, terms_as_of(vdate, vsrc), bool(review), review, vid))

    for cid in {card_id, old_card}:
        set_latest(conn, cid)
    if old_card != card_id and not conn.execute("SELECT 1 FROM card_versions WHERE card_id=%s", (old_card,)).fetchone():
        old = conn.execute("SELECT json_path FROM card_versions WHERE version_id=%s", (vid,)).fetchone()
        conn.execute("DELETE FROM cards WHERE card_id=%s", (old_card,))
        if old and old["json_path"] and Path(old["json_path"]).exists():
            Path(old["json_path"]).unlink()
    return write_card(conn, cfg, card_id, prompt_version)


def set_latest(conn, card_id: int) -> None:
    conn.execute("UPDATE card_versions SET is_latest=false WHERE card_id=%s", (card_id,))
    conn.execute(
        """UPDATE card_versions SET is_latest=true WHERE version_id = (
             SELECT version_id FROM card_versions WHERE card_id=%s
             ORDER BY version_date DESC NULLS LAST, version_id DESC LIMIT 1)""", (card_id,))


def _found(r: dict):
    return r.get("raw_value") if r.get("status") == "found" and r.get("raw_value") else None


def _json(v):
    return None if v is None else json.dumps(v)


def fields_by_name(spec: dict) -> dict:
    return {n: {**f, "_group": g} for g, gd in spec["groups"].items() for n, f in gd["fields"].items()}


def extract_version(conn, cfg, model, spec, examples, v, run_id, log):
    """Extract one version. Returns (json path, new sibling versions, found, total, regulations)."""
    groups = spec["groups"]
    docs, pages = version_context(conn, v["version_id"])
    if not pages:
        raise ValueError("no readable pages (all pages still need OCR?)")
    meta = {"version_id": v["version_id"]}
    siblings = []
    if v.get("product_name") is None and not v.get("products_in_group"):
        products = list_products(model, docs, pages, meta)
        if len(products) > 1:
            siblings = split_multi_card(conn, v, products, log)
        else:
            conn.execute("UPDATE card_versions SET products_in_group=%s WHERE version_id=%s",
                         (products, v["version_id"]))
    product, products = v.get("product_name"), v.get("products_in_group") or []
    doc_map = {d["label"]: (d["sha256"], d["file_name"]) for d in docs}
    results = {}
    for gname, gdef in groups.items():
        data, stats = extract_group(model, gdef, gdef["fields"], docs, pages, group=gname, product=product,
                                    products=products, examples=examples, cfg=cfg, meta={**meta, "group": gname})
        for fname, fdef in gdef["fields"].items():
            results[fname] = clean_field(fname, data.get(fname) or {"status": "ambiguous"}, fdef["normalized"], doc_map)
        log.info("%s: %.1fs, pages %s", gname, stats["seconds"], ",".join(stats["pages_sent"]) or "none matched")
    regs, _ = extract_regulations(conn, model, spec["regulations"], docs, pages, meta)
    out = save_version(conn, cfg, v, docs, pages, fields_by_name(spec), results, regs, run_id, spec["prompt_version"])
    found = sum(r["status"] in ("found", "stated_none") for r in results.values())
    return out, siblings, found, len(results), len(regs)


def run(cfg: Config, version_ids: list[int] | None = None, redo: bool = False) -> dict:
    log = setup_logging(cfg, "extract")
    spec = load_fields()
    examples = load_examples()
    model = cfg.section("models").get("extract", "qwen3.5:4b")
    counts = {"versions": 0, "split_multi_card": 0, "failed": 0}

    with connect(cfg.database_url) as conn:
        run_id = start_run(conn, cfg, "extract", spec["prompt_version"])
        llm.configure(cfg, "extract", run_id)
        where = "v.quarter = %s AND " + ("v.stage IN ('grouped','extracted','validated','embedded')" if redo
                                         else "v.stage = 'grouped'")
        params = [cfg.quarter]
        if version_ids:
            where += " AND v.version_id = ANY(%s)"
            params.append(version_ids)
        todo = conn.execute(f"SELECT v.* FROM card_versions v WHERE {where} ORDER BY v.version_id", params).fetchall()
        log.info("%d card versions to extract with %s (prompt %s, %d worked-example groups)", len(todo), model,
                 spec["prompt_version"], len(examples))
        n = 0
        progress.start(len(todo), "versions")
        while todo:
            v = todo.pop(0)
            n += 1
            progress.step(n, f"v{v['version_id']}" + (f" {v['product_name'][:30]}" if v.get("product_name") else ""))
            try:
                out, siblings, found, total, nregs = extract_version(conn, cfg, model, spec, examples, v, run_id, log)
                conn.commit()
                counts["versions"] += 1
                if siblings:
                    counts["split_multi_card"] += 1
                    todo = siblings + todo
                    progress.grow(len(siblings))
                log.info("version %d%s done: %d/%d fields found, %d regulations -> %s", v["version_id"],
                         f" ({v['product_name']})" if v.get("product_name") else "", found, total, nregs, out)
            except Exception as e:  # noqa: BLE001 - log and continue with the next version
                conn.rollback()
                counts["failed"] += 1
                log.exception("version %d FAILED: %s", v["version_id"], e)
        finish_run(conn, run_id)
    llm.unload(model)
    progress.stop()
    log.info("extract done: %s", counts)
    return counts
