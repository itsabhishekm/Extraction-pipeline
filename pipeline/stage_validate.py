"""Stage 6 - validate (the Validation Gate), and stage 7 - retry (the Repair Agent).

validate, for each version at stage 'extracted':
  per value (status found / stated_none)
    schema check     normalized_value matches its format in fields.yaml
    quote grounding  the quote is on the cited page (normalized; approximate match allowed, looser on
                     OCR pages); found on another page of the same PDF -> page corrected
    number check     every number in raw_value is in the quote (pricing and late-fee fields)
    business rules   plausible ranges (config validate.ranges)
    model check      Qwen (thinking on) confirms the quote supports the value
  per version
    cross-field      no arbitration clause -> opt-out not_applicable (set by rule); a jury waiver
                     "within_arbitration" or a military exception needs an arbitration clause
    quality gates    arbitration found but opt-out not_present; daily periodic rate x 365 matches no
                     APR; multi-card: products listed vs. products with a purchase APR
    double read      a second, independent read of the pricing fields, one call per PDF, thinking on.
                     Disagrees with the first read -> retry. Two PDFs of the same version give
                     different values -> a conflicts row + human review (never picked automatically)
Passing values -> 'passed'; failing -> 'retry_queued' and listed in retry_queue.jsonl.

retry: re-asks ONLY the queued values, thinking on, more text, the failure reason, and page images
for pricing fields (vision fallback for jumbled tables). After `max_retries` failed retries ->
'human_review' and a review_items row.
"""
import json
import re
from difflib import SequenceMatcher

import jsonschema

from . import llm
from .config import Config, progress, setup_logging
from .db import connect, finish_run, start_run
from .json_out import write_card
from .pages import normalize
from .postprocess import NUM, clean_field, display_for, is_rule_value
from .stage_extract import extract_group, fields_by_name, load_examples, load_fields, version_context

CHECK_SYSTEM = """You check facts extracted from U.S. credit card agreements.
For each item you get a field, the extracted value and the quote it came from.
Answer supported = true when the quote states the value: the right number, the right fee or rate,
the right meaning. The value does NOT need to cover every detail the field could have; a missing
detail (no intro rate, no duration, no index, no repeat amount) is not a reason to reject.
Answer supported = false ONLY when:
- the quote contradicts the value (different number, different state, yes vs. no), or
- the quote is about a different thing (e.g. a cash advance fee quoted for the balance transfer fee), or
- the quote does not mention the field at all.
A "notice of claim" clause is not arbitration; a general Military Lending Act notice is not an
arbitration exception. Give a one-line reason."""


DEFAULT_RANGES = {
    "purchase_apr": [0, 40], "balance_transfer_apr": [0, 40], "cash_advance_apr": [0, 45], "penalty_apr": [0, 40],
    "balance_transfer_fee.pct": [0, 10], "cash_advance_fee.pct": [0, 10], "late_fee": [0, 50],
    "returned_payment_fee": [0, 50], "annual_fee": [0, 1000], "foreign_transaction_fee": [0, 5],
    "minimum_interest_charge": [0, 5], "payment_due_days": [21, 60], "late_fee_grace_days": [0, 30],
}
DPR = re.compile(r"daily periodic rate[^0-9]{0,60}?(\d+\.\d+)\s*%", re.I)
APR_IN_TEXT = re.compile(r"(\d{1,2}\.\d{1,2})\s*%")
GATE_PREFIXES = ("quality gate:", "daily periodic rate")


# ----------------------------------------------------------------------------- value checks
def quote_check(quote: str, page_text: str, ocr: bool) -> tuple[bool, str]:
    q, t = normalize(quote), normalize(page_text)
    if not q:
        return False, "empty quote"
    if q in t:
        return True, ""
    m = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    ratio = m.size / len(q)
    if ratio >= (0.75 if ocr else 0.85):
        return True, f"approximate quote match ({ratio:.0%})"
    return False, f"quote not found on the cited page (best match {ratio:.0%})"


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip("0").rstrip(".") if "." in n else n.replace(",", "")
            for n in NUM.findall(text or "")}


def schema_check(fv: dict, fdef: dict) -> list[str]:
    norm = fdef["normalized"]
    if fv["normalized_value"] is None or norm.get("type") == "null":
        return []
    try:
        jsonschema.validate(fv["normalized_value"], norm)
        return []
    except jsonschema.ValidationError as e:
        return [f"normalized_value does not match its format: {e.message[:150]}"]


def range_check(fv: dict, ranges: dict) -> list[str]:
    name, val = fv["field_name"], fv["normalized_value"]
    errs = []
    for key, (lo, hi) in ranges.items():
        fname, _, sub = key.partition(".")
        if fname != name or val is None:
            continue
        nums = []
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            nums = [val]
        elif isinstance(val, dict):
            picks = [sub] if sub else ["min", "max", "intro_apr", "first_usd", "repeat_usd", "max_usd",
                                       "min_usd", "first_year_usd", "usd", "days", "pct"]
            nums = [val.get(k) for k in picks
                    if isinstance(val.get(k), (int, float)) and not isinstance(val.get(k), bool)]
        bad = [n for n in nums if not lo <= n <= hi]
        if bad:
            errs.append(f"business rule: value {bad} outside the plausible range {lo}-{hi}")
    return errs


def code_checks(fv, pages_by_doc, fdef, ranges) -> tuple[list[str], list[str], int | None]:
    """Return (errors, notes, corrected_page)."""
    errors, notes, new_page = schema_check(fv, fdef) + range_check(fv, ranges), [], None
    doc_pages = pages_by_doc.get(fv["source_sha256"], {})
    if not fv["quote"]:
        return errors + ["no quote given"], notes, None
    page = doc_pages.get(fv["page_no"])
    if page is None:
        errors.append(f"page {fv['page_no']} does not exist in the cited PDF")
    else:
        ok, note = quote_check(fv["quote"], page.text, page.ocr)
        if ok:
            if note:
                notes.append(note)
        else:
            for other_no, other in doc_pages.items():
                if other_no != fv["page_no"] and quote_check(fv["quote"], other.text, other.ocr)[0]:
                    new_page = other_no
                    notes.append(f"page corrected from {fv['page_no']} to {other_no}")
                    break
            else:
                errors.append(note)
    if fdef["_group"] in ("pricing", "late_fee_rules") and fv["status"] == "found" and fv["raw_value"]:
        missing = numbers_in(fv["raw_value"]) - numbers_in(fv["quote"])
        if missing:
            errors.append(f"numbers {sorted(missing)} in the value are not in the quote")
    return errors, notes, new_page


def model_check(model: str, items: list[dict], fields_def: dict, meta: dict) -> dict:
    """items: field_values rows. Returns {field_name: (supported, reason)}."""
    if not items:
        return {}
    lines = [json.dumps({"field": fv["field_name"], "means": fields_def[fv["field_name"]]["question"],
                         "value": fv["raw_value"], "normalized": fv["normalized_value"], "status": fv["status"],
                         "quote": fv["quote"]}, ensure_ascii=False) for fv in items]
    schema = {"type": "object", "required": [fv["field_name"] for fv in items],
              "properties": {fv["field_name"]: {"type": "object", "required": ["supported", "reason"],
                                                "properties": {"supported": {"type": "boolean"},
                                                               "reason": {"type": "string"}}}
                             for fv in items}}
    data, _ = llm.extract_json(model, CHECK_SYSTEM, "ITEMS:\n" + "\n".join(lines), schema, think=True,
                               kind="check", meta=meta)
    return {k: (bool(v.get("supported")), v.get("reason", "")) for k, v in data.items() if isinstance(v, dict)}


# ----------------------------------------------------------------------------- version checks
def cross_field_rules(conn, rows: dict) -> dict:
    """Fix or flag combinations the audit prompt rules out. Returns {field: [errors]}."""
    errs = {}
    arb = rows.get("arbitration_clause")
    has_arb = arb is not None and arb["status"] == "found" and arb["normalized_value"] is True
    no_arb = arb is not None and (arb["status"] in ("not_present", "stated_none")
                                  or (arb["status"] == "found" and arb["normalized_value"] is False))
    opt = rows.get("arbitration_opt_out")
    if no_arb and opt is not None and opt["status"] != "not_applicable":
        conn.execute(
            """UPDATE field_values SET status='not_applicable', raw_value=NULL, normalized_value=NULL, quote=NULL,
                 source_sha256=NULL, page_no=NULL, display=%s,
                 note=concat_ws('; ', note, 'set by rule: no arbitration clause') WHERE value_id=%s""",
            (display_for("not_applicable", None), opt["value_id"]))
        opt.update(status="not_applicable", normalized_value=None, quote=None, raw_value=None,
                   note=((opt["note"] + "; ") if opt["note"] else "") + "set by rule: no arbitration clause")
    if has_arb and opt is not None and opt["status"] == "not_present":
        errs.setdefault("arbitration_opt_out", []).append(
            "quality gate: arbitration clause found but no opt-out answer; check the arbitration section")
    jury = rows.get("jury_trial_waiver")
    if jury and jury["normalized_value"] == "within_arbitration" and not has_arb:
        errs.setdefault("jury_trial_waiver", []).append(
            "business rule: jury waiver 'within_arbitration' but no arbitration clause")
    mil = rows.get("military_exception")
    if mil and mil["normalized_value"] is True and not has_arb:
        errs.setdefault("military_exception", []).append(
            "business rule: military exception found but no arbitration clause")
    return errs


def dpr_check(pages, rows: dict) -> list[str]:
    """Quality gate: each daily periodic rate x 365 should match an APR (+/-0.01, allowing for
    the DPR being rounded in print)."""
    flags = []
    aprs = set()
    for name in ("purchase_apr", "balance_transfer_apr", "cash_advance_apr", "penalty_apr"):
        val = (rows.get(name) or {}).get("normalized_value") or {}
        if isinstance(val, dict):
            aprs |= {v for k, v in val.items() if k in ("min", "max") and isinstance(v, (int, float))}
    for p in pages:
        aprs |= {float(a) for a in APR_IN_TEXT.findall(p.text)}
    for p in pages:
        for m in DPR.finditer(p.text):
            dpr = float(m.group(1))
            decimals = len(m.group(1).split(".")[1])
            tol = 0.01 + 0.5 * 10 ** -decimals * 365
            if aprs and not any(abs(dpr * 365 - a) <= tol for a in aprs):
                flags.append(f"daily periodic rate {dpr}% x 365 = {dpr * 365:.2f}% matches no APR "
                             f"({p.file_name} page {p.page_no})")
    return flags


def same_value(a, b) -> bool:
    """Compare two normalized values; keys null on either side are ignored; numbers to 2 decimals."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        keys = [k for k in a if k in b and a[k] is not None and b[k] is not None]
        return all(same_value(a[k], b[k]) for k in keys)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return round(float(a), 2) == round(float(b), 2)
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return str(a).strip().lower() == str(b).strip().lower()


def double_read(conn, cfg, model, spec, v, docs, pages, rows: dict, meta) -> tuple[dict, dict]:
    """Second, independent read of the double-read groups, one call per PDF with matching pages.
    Returns (retry_errors, human_errors) as {field: [errors]}; writes conflicts rows."""
    retry, human = {}, {}
    groups = cfg.section("validate").get("double_read_groups", ["pricing"])
    conn.execute("DELETE FROM conflicts WHERE version_id=%s AND NOT resolved", (v["version_id"],))
    for gname in groups:
        gdef = spec["groups"][gname]
        kws = [k.lower() for k in gdef.get("keywords", [])]
        per_doc = {}
        for d in docs:
            dpages = [p for p in pages if p.doc == d["label"]]
            if kws and not any(k in p.text.lower() for p in dpages for k in kws):
                continue
            data, _ = extract_group(model, gdef, gdef["fields"], [d], dpages, group=gname,
                                    product=v.get("product_name"), products=v.get("products_in_group") or [],
                                    meta={**meta, "group": gname, "double_read": d["label"]})
            dmap = {d["label"]: (d["sha256"], d["file_name"])}
            per_doc[d["sha256"]] = {f: clean_field(f, data.get(f) or {"status": "ambiguous"},
                                                   gdef["fields"][f]["normalized"], dmap) for f in gdef["fields"]}
        for fname in gdef["fields"]:
            first = rows.get(fname)
            if first is None or is_rule_value(fname, first["note"]):
                continue
            reads = {sha: r[fname] for sha, r in per_doc.items()}
            found = {sha: r for sha, r in reads.items() if r["status"] in ("found", "stated_none")}
            shas = list(found)
            for i in range(len(shas)):
                for j in range(i + 1, len(shas)):
                    a, b = found[shas[i]], found[shas[j]]
                    if not same_value(a["normalized_value"], b["normalized_value"]):
                        conn.execute(
                            """INSERT INTO conflicts (version_id, field_name, value_a, sha256_a, page_a,
                                 value_b, sha256_b, page_b) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (v["version_id"], fname, a["raw_value"], shas[i], a["page"], b["raw_value"], shas[j],
                             b["page"]))
                        human.setdefault(fname, []).append(
                            f"conflict: {a['raw_value']!r} in {a['source_pdf']} vs {b['raw_value']!r} in {b['source_pdf']}")
            if fname in human:
                continue
            if first["status"] in ("found", "stated_none"):
                second = reads.get(first["source_sha256"])
                if second is None:
                    continue
                if second["status"] not in ("found", "stated_none"):
                    retry.setdefault(fname, []).append(f"double read did not find this value (said {second['status']})")
                elif not same_value(first["normalized_value"], second["normalized_value"]):
                    retry.setdefault(fname, []).append(
                        f"double read disagrees: second read gave {second['raw_value']!r} = "
                        f"{json.dumps(second['normalized_value'])}")
            elif found and first["status"] not in ("not_applicable",):
                second = next(iter(found.values()))
                retry.setdefault(fname, []).append(
                    f"first read said {first['status']} but the double read found {second['raw_value']!r}")
    return retry, human


def multi_card_gate(conn, v) -> list[str]:
    """Quality gate: when every product of a multi-card document is extracted, each must have a purchase APR."""
    products = v.get("products_in_group") or []
    if len(products) < 2:
        return []
    base = v["group_key"].split("#")[0]
    r = conn.execute(
        """SELECT count(*) FILTER (WHERE stage IN ('extracted','validated','embedded')) AS done,
                  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM field_values fv WHERE fv.version_id = cv.version_id
                      AND fv.field_name='purchase_apr' AND fv.status IN ('found','stated_none'))) AS with_apr
           FROM card_versions cv WHERE quarter=%s AND split_part(group_key, '#', 1)=%s""",
        (v["quarter"], base)).fetchone()
    if r["done"] >= len(products) and r["with_apr"] < len(products):
        return [f"quality gate: multi-card document lists {len(products)} products but only "
                f"{r['with_apr']} have a purchase APR"]
    return []


# ----------------------------------------------------------------------------- shared machinery
def _pages_by_doc(pages):
    out = {}
    for p in pages:
        out.setdefault(p.sha256, {})[p.page_no] = p
    return out


def _apply(conn, rows, results, retry_errs, human_errs):
    """Write validation states. Returns [(row, errors)] for values that did not pass."""
    failed = []
    for fv in rows:
        name = fv["field_name"]
        errs, notes, new_page = results.get(fv["value_id"], ([], [], None))
        errs = errs + retry_errs.get(name, [])
        human = human_errs.get(name, [])
        state = "human_review" if human else "retry_queued" if errs else "passed"
        if fv["validation"] == "human_approved":
            continue
        all_errs = errs + human
        note = "; ".join(filter(None, [fv["note"]] + notes)) or None
        conn.execute("""UPDATE field_values SET validation=%s, validation_errors=%s, note=%s,
                          page_no=COALESCE(%s, page_no) WHERE value_id=%s""",
                     (state, all_errs, note, new_page, fv["value_id"]))
        fv["validation"] = state
        if human:
            conn.execute("DELETE FROM review_items WHERE value_id=%s AND decision IS NULL", (fv["value_id"],))
            conn.execute("INSERT INTO review_items (value_id, reason) VALUES (%s, %s)", (fv["value_id"], "; ".join(human)))
        if all_errs:
            failed.append((fv, all_errs))
    return failed


def check_version(conn, cfg, model, spec, v, rows_list, docs, pages, use_llm, log, run_double=True,
                  only_ids=None):
    """Run the whole gate on one version. `only_ids`: validate just these values (retry), but the
    cross-field rules still see all values. Returns (failed, version_flags)."""
    fields_def = fields_by_name(spec)
    ranges = {**DEFAULT_RANGES, **cfg.section("validate").get("ranges", {})}
    pbd = _pages_by_doc(pages)
    meta = {"version_id": v["version_id"]}
    rows = {fv["field_name"]: fv for fv in rows_list}
    retry = cross_field_rules(conn, rows)
    apr = rows.get("purchase_apr")
    if apr is not None and apr["status"] not in ("found", "stated_none", "not_applicable") \
            and any("interest rates and interest charges" in p.text.lower() for p in pages):
        retry.setdefault("purchase_apr", []).append(
            "quality gate: purchase APR missing but the PDF has an Interest Rates and Interest Charges table")
    targets = [fv for fv in rows_list if only_ids is None or fv["value_id"] in only_ids]
    results, to_model = {}, []
    for fv in targets:
        if fv["status"] not in ("found", "stated_none") or is_rule_value(fv["field_name"], fv["note"]):
            continue
        fdef = fields_def[fv["field_name"]]
        errs, notes, new_page = code_checks(fv, pbd, fdef, ranges)
        results[fv["value_id"]] = (errs, notes, new_page)
        if not errs and fdef["_group"] != "identity":
            to_model.append(fv)
    if use_llm and to_model:
        try:
            for name, (ok, reason) in model_check(model, to_model, fields_def, meta).items():
                fv = rows.get(name)
                if fv and not ok and fv["value_id"] in results:
                    results[fv["value_id"]][0].append(f"model check: {reason or 'quote does not support the value'}")
        except Exception as e:  # noqa: BLE001 - a failed check call should not block validation
            log.warning("v%d model check failed, code checks only: %s", v["version_id"], e)
    human = {}
    if run_double and cfg.section("validate").get("double_read", True):
        try:
            r2, h2 = double_read(conn, cfg, model, spec, v, docs, pages, rows, meta)
            for k, e in r2.items():
                retry.setdefault(k, []).extend(e)
            human = h2
        except Exception as e:  # noqa: BLE001
            log.warning("v%d double read failed: %s", v["version_id"], e)
    failed = _apply(conn, targets, results, retry, human)
    flags = dpr_check(pages, rows) + multi_card_gate(conn, v)
    return failed, flags


def _write_queue(conn, cfg):
    rows = conn.execute(
        """SELECT fv.version_id, fv.field_name, fv.attempts, fv.validation_errors, c.card_name, i.folder_name
           FROM field_values fv JOIN card_versions v USING (version_id) JOIN cards c USING (card_id)
           JOIN issuers i USING (issuer_id)
           WHERE fv.validation = 'retry_queued' AND v.quarter = %s ORDER BY 1, 2""", (cfg.quarter,)).fetchall()
    path = cfg.data_root / "retry_queue.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"version_id": r["version_id"], "field": r["field_name"], "attempt": r["attempts"],
                                "errors": r["validation_errors"], "card": r["card_name"],
                                "issuer": r["folder_name"]}, ensure_ascii=False) + "\n")
    return path, len(rows)


def finish_version(conn, version_id, flags=None):
    """Stage -> validated, and rebuild review_reasons from the current values:
    unreadable / ambiguous / low-confidence values (unless a person decided them), values still in
    retry or human review, market_scope non_us / unclear, and the version flags (quality gates).
    flags=None keeps the version flags already recorded (used by human review)."""
    v = conn.execute("SELECT review_reasons, market_scope FROM card_versions WHERE version_id=%s",
                     (version_id,)).fetchone()
    old_flags = [r for r in (v["review_reasons"] or []) if r.startswith(GATE_PREFIXES)]
    rows = conn.execute("SELECT field_name, status, confidence, validation FROM field_values WHERE version_id=%s "
                        "ORDER BY field_name", (version_id,)).fetchall()
    reasons = []
    for r in rows:
        if r["validation"] in ("retry_queued", "human_review"):
            reasons.append(f"{r['field_name']}: {r['validation']}")
        elif r["validation"] == "human_approved":
            continue
        elif r["status"] in ("unreadable", "ambiguous"):
            reasons.append(f"{r['field_name']}: {r['status']}")
        elif r["confidence"] == "low" and r["status"] in ("found", "stated_none"):
            reasons.append(f"{r['field_name']}: low confidence")
    if v["market_scope"] in ("non_us", "unclear"):
        reasons.append(f"market_scope {v['market_scope']}")
    reasons += old_flags if flags is None else list(flags)
    reasons = list(dict.fromkeys(reasons))
    conn.execute(
        """UPDATE card_versions SET stage = CASE WHEN stage = 'extracted' THEN 'validated' ELSE stage END,
             review_reasons=%s, needs_manual_review=%s WHERE version_id=%s""",
        (reasons, bool(reasons), version_id))


def check_regulations(conn, version_id, pages_by_doc):
    """Quote-check regulation mentions (no retry: a mention that fails is marked 'failed')."""
    passed = failed = 0
    for m in conn.execute("SELECT * FROM regulation_mentions WHERE version_id=%s", (version_id,)).fetchall():
        page = pages_by_doc.get(m["source_sha256"], {}).get(m["page_no"])
        ok = page is not None and quote_check(m["quote"], page.text, page.ocr)[0]
        conn.execute("UPDATE regulation_mentions SET validation=%s WHERE mention_id=%s",
                     ("passed" if ok else "failed", m["mention_id"]))
        passed += ok
        failed += not ok
    return passed, failed


# ----------------------------------------------------------------------------- stages
def reset_versions(conn, cfg) -> int:
    """validate --redo: put validated versions back to 'extracted' and clear their validation results
    (open review items and unresolved conflicts too). Values a person already decided are kept."""
    vids = [r["version_id"] for r in conn.execute(
        "SELECT version_id FROM card_versions WHERE quarter=%s AND stage IN ('validated','embedded')",
        (cfg.quarter,)).fetchall()]
    if not vids:
        return 0
    conn.execute("""DELETE FROM review_items WHERE decision IS NULL AND value_id IN
                    (SELECT value_id FROM field_values WHERE version_id = ANY(%s))""", (vids,))
    conn.execute("DELETE FROM conflicts WHERE NOT resolved AND version_id = ANY(%s)", (vids,))
    conn.execute("""UPDATE field_values SET validation='pending', validation_errors='{}', attempts=1
                    WHERE version_id = ANY(%s) AND validation <> 'human_approved' AND field_name <> 'submission_id'""",
                 (vids,))
    conn.execute("UPDATE regulation_mentions SET validation='pending' WHERE version_id = ANY(%s)", (vids,))
    conn.execute("UPDATE card_versions SET stage='extracted' WHERE version_id = ANY(%s)", (vids,))
    conn.commit()
    return len(vids)


def run(cfg: Config, redo: bool = False) -> dict:
    log = setup_logging(cfg, "validate")
    spec = load_fields()
    model = cfg.section("models").get("extract", "qwen3.5:4b")
    vcfg = cfg.section("validate")
    use_llm = vcfg.get("llm_check", True)
    counts = {"versions": 0, "values_checked": 0, "passed": 0, "queued_for_retry": 0, "to_human_review": 0,
              "version_flags": 0, "regulations_passed": 0, "regulations_failed": 0}
    with connect(cfg.database_url) as conn:
        if redo:
            log.info("--redo: %d validated versions reset to be validated again", reset_versions(conn, cfg))
        run_id = start_run(conn, cfg, "validate", spec["prompt_version"])
        llm.configure(cfg, "validate", run_id)
        versions = conn.execute("SELECT * FROM card_versions WHERE quarter=%s AND stage='extracted' "
                                "ORDER BY version_id", (cfg.quarter,)).fetchall()
        log.info("%d versions to validate (model check %s, double read %s)", len(versions),
                 "on" if use_llm else "off", "on" if vcfg.get("double_read", True) else "off")
        progress.start(len(versions), "versions")
        for vi, v in enumerate(versions, start=1):
            progress.step(vi, f"v{v['version_id']}")
            try:
                docs, pages = version_context(conn, v["version_id"])
                rows = conn.execute("SELECT * FROM field_values WHERE version_id=%s", (v["version_id"],)).fetchall()
                failed, flags = check_version(conn, cfg, model, spec, v, rows, docs, pages, use_llm, log)
                rp, rf = check_regulations(conn, v["version_id"], _pages_by_doc(pages))
                counts["regulations_passed"] += rp
                counts["regulations_failed"] += rf
                checked = sum(r["status"] in ("found", "stated_none") for r in rows)
                human = sum(1 for fv, _ in failed if fv["validation"] == "human_review")
                counts["values_checked"] += checked
                counts["queued_for_retry"] += len(failed) - human
                counts["to_human_review"] += human
                counts["passed"] += sum(r["validation"] == "passed" for r in rows)
                counts["version_flags"] += len(flags)
                for fv, errs in failed:
                    log.info("%s -> %s: %s", fv["field_name"], fv["validation"], "; ".join(errs))
                for f in flags:
                    log.info("flag: %s", f)
                finish_version(conn, v["version_id"], flags)
                write_card(conn, cfg, v["card_id"], spec["prompt_version"])
                conn.commit()
                counts["versions"] += 1
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                log.exception("v%d validation FAILED: %s", v["version_id"], e)
        path, n = _write_queue(conn, cfg)
        finish_run(conn, run_id)
    llm.unload(model)
    progress.stop()
    log.info("validate done: %s; retry queue: %d values in %s", counts, n, path)
    return counts


def run_retry(cfg: Config) -> dict:
    log = setup_logging(cfg, "retry")
    spec = load_fields()
    examples = load_examples()
    groups = spec["groups"]
    fields_def = fields_by_name(spec)
    model = cfg.section("models").get("extract", "qwen3.5:4b")
    vcfg = cfg.section("validate")
    use_llm = vcfg.get("llm_check", True)
    max_retries = vcfg.get("max_retries", 2)
    vision = vcfg.get("vision_fallback", True)
    counts = {"retried": 0, "now_passed": 0, "still_queued": 0, "to_human_review": 0}
    with connect(cfg.database_url) as conn:
        run_id = start_run(conn, cfg, "retry", spec["prompt_version"])
        llm.configure(cfg, "retry", run_id)
        queued = conn.execute(
            """SELECT fv.*, v.card_id FROM field_values fv JOIN card_versions v USING (version_id)
               WHERE fv.validation='retry_queued' AND v.quarter=%s ORDER BY fv.version_id""",
            (cfg.quarter,)).fetchall()
        by_version = {}
        for fv in queued:
            by_version.setdefault(fv["version_id"], []).append(fv)
        log.info("%d values in %d versions to retry (vision fallback %s)", len(queued), len(by_version),
                 "on" if vision else "off")
        progress.start(len(by_version), "versions")
        for vi, (vid, items) in enumerate(by_version.items(), start=1):
            progress.step(vi, f"v{vid} ({len(items)} values)")
            try:
                v = conn.execute("SELECT * FROM card_versions WHERE version_id=%s", (vid,)).fetchone()
                docs, pages = version_context(conn, vid)
                doc_map = {d["label"]: (d["sha256"], d["file_name"]) for d in docs}
                by_group = {}
                for fv in items:
                    by_group.setdefault(fields_def[fv["field_name"]]["_group"], []).append(fv)
                for gname, fvs in by_group.items():
                    gdef = groups[gname]
                    sub = {fv["field_name"]: gdef["fields"][fv["field_name"]] for fv in fvs}
                    feedback = "\n".join(f"- {fv['field_name']}: {'; '.join(fv['validation_errors'] or [])}"
                                         for fv in fvs)
                    data, _ = extract_group(model, gdef, sub, docs, pages, group=gname, think=True,
                                            max_chars=int(gdef.get("max_chars", 14000) * 1.5), feedback=feedback,
                                            product=v["product_name"], products=v["products_in_group"] or [],
                                            examples=examples, cfg=cfg, use_images=vision and gname == "pricing",
                                            meta={"version_id": vid, "group": gname, "retry": True})
                    for fv in fvs:
                        r = clean_field(fv["field_name"], data.get(fv["field_name"]) or {"status": "ambiguous"},
                                        sub[fv["field_name"]]["normalized"], doc_map)
                        ev = r["status"] in ("found", "stated_none")
                        conn.execute(
                            """UPDATE field_values SET status=%s, raw_value=%s, normalized_value=%s::jsonb, quote=%s,
                                 source_sha256=%s, page_no=%s, display=%s, confidence=%s, note=%s,
                                 attempts=attempts+1, run_id=%s WHERE value_id=%s""",
                            (r["status"], r["raw_value"], None if r["normalized_value"] is None
                             else json.dumps(r["normalized_value"]), r["quote"] if ev else None,
                             r["source_sha256"] if ev else None, r["page"] if ev else None, r["display"],
                             r["confidence"], r["note"] or None, run_id, fv["value_id"]))
                        counts["retried"] += 1
                retried = {fv["value_id"] for fv in items}
                all_rows = conn.execute("SELECT * FROM field_values WHERE version_id=%s", (vid,)).fetchall()
                failed, flags = check_version(conn, cfg, model, spec, v, all_rows, docs, pages, use_llm, log,
                                              run_double=False, only_ids=retried)
                failed_errs = {fv["value_id"]: errs for fv, errs in failed}
                for fv in conn.execute("SELECT * FROM field_values WHERE value_id = ANY(%s)",
                                       (list(retried),)).fetchall():
                    if fv["value_id"] not in failed_errs:
                        counts["now_passed"] += 1
                    elif fv["validation"] == "human_review":
                        counts["to_human_review"] += 1
                    elif fv["attempts"] - 1 >= max_retries:
                        conn.execute("UPDATE field_values SET validation='human_review' WHERE value_id=%s",
                                     (fv["value_id"],))
                        conn.execute("INSERT INTO review_items (value_id, reason) VALUES (%s, %s)",
                                     (fv["value_id"], f"failed after {max_retries} retries: "
                                      + "; ".join(failed_errs[fv["value_id"]])))
                        counts["to_human_review"] += 1
                    else:
                        counts["still_queued"] += 1
                finish_version(conn, vid, flags)
                write_card(conn, cfg, items[0]["card_id"], spec["prompt_version"])
                conn.commit()
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                log.exception("v%d retry FAILED: %s", vid, e)
        path, n = _write_queue(conn, cfg)
        finish_run(conn, run_id)
    llm.unload(model)
    progress.stop()
    log.info("retry done: %s; %d values still queued in %s", counts, n, path)
    return counts
