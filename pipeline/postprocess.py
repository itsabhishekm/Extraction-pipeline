"""Code-side rules applied to every model answer (audit prompt: STATUS VALUES,
NORMALIZED VALUE FORMATS, DATA SCOPE item 4), plus the per-version derived fields."""
import re
from datetime import date, datetime

STATUSES = ["found", "stated_none", "not_applicable", "not_present", "unreadable", "ambiguous"]
YES_NO_LEGAL = {"arbitration_clause", "class_action_waiver", "military_exception", "small_claims_carveout"}
DATA_SCOPE = ("Consumer credit card terms as submitted by the issuer to the CFPB. Covers cards offered to new "
              "applicants; it may not include discontinued cards or issuers with fewer than 10,000 accounts. "
              "Not verified by the CFPB.")
NUM = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
DEFAULT_NOTE = "default: not stated"


def is_rule_value(field_name: str, note: str | None) -> bool:
    """Values set by a rule, not read from the text: nothing to quote-check."""
    return field_name == "submission_id" or DEFAULT_NOTE in (note or "")


def display_for(status: str, raw_value) -> str:
    return {
        "found": str(raw_value) if raw_value not in (None, "") else "Found in the submitted agreement",
        "stated_none": "None (stated in the agreement)",
        "not_applicable": "Not applicable to this card",
        "not_present": "Not found in the submitted agreement",
    }.get(status, "Could not be determined from the submitted agreement")


def _types(schema: dict) -> list:
    t = schema.get("type", [])
    return t if isinstance(t, list) else [t]


def coerce(value, schema: dict):
    """Fix common small-model slips: "18.49%" for a number, "true" for a boolean, etc."""
    if value is None:
        return None
    types = _types(schema)
    if "object" in types and isinstance(value, dict):
        props = schema.get("properties")
        if props:
            return {k: coerce(value.get(k), props[k]) for k in props}
        extra = schema.get("additionalProperties")
        return {k: coerce(v, extra) for k, v in value.items()} if isinstance(extra, dict) else value
    if "array" in types and isinstance(value, list):
        return [coerce(v, schema.get("items", {})) for v in value]
    if ("number" in types or "integer" in types) and isinstance(value, str):
        m = NUM.search(value)
        if not m:
            return None
        n = float(m.group().replace(",", ""))
        return int(n) if "integer" in types else n
    if "integer" in types and isinstance(value, float):
        return int(value)
    if "boolean" in types and isinstance(value, str):
        return {"true": True, "yes": True, "false": False, "no": False}.get(value.strip().lower())
    return value


def _none_value(schema: dict):
    types = _types(schema)
    if "number" in types or "integer" in types:
        return 0
    if "boolean" in types:
        return False
    if "string" in types and "none" in (schema.get("enum") or []):
        return "none"
    if "object" in types and schema.get("properties"):
        return {k: (0 if {"number", "integer"} & set(_types(s)) else None) for k, s in schema["properties"].items()}
    return None


def clean_field(name: str, ans: dict, schema: dict, doc_map: dict) -> dict:
    """Apply the status rules to one field answer. doc_map: label (D1) -> (sha256, file_name)."""
    status = ans.get("status") if ans.get("status") in STATUSES else "ambiguous"
    raw = ans.get("raw_value")
    if isinstance(raw, str):  # drop markdown the model copied from the .md text
        raw = re.sub(r"^[#>\-\s]+|\*\*|__", "", raw).strip() or None
    norm = coerce(ans.get("normalized_value"), schema)
    quote = (ans.get("quote") or "").strip() or None
    doc = ans.get("doc")
    page = ans.get("page") if isinstance(ans.get("page"), int) else coerce(ans.get("page"), {"type": "integer"})
    conf = ans.get("confidence") if ans.get("confidence") in ("high", "medium", "low") else "low"
    note = ""

    if status not in ("found", "stated_none") and isinstance(raw, str) and \
            raw.strip().lower() in ("none", "null", "n/a", "not present", "not stated", "not mentioned"):
        raw = None  # the model wrote the word for "nothing" as the value; there is no value
    if status == "stated_none":
        norm = _none_value(schema) if norm in (None, {}, []) else norm
    elif status != "found":
        norm = False if name in YES_NO_LEGAL and status == "not_present" else None
        if status in ("not_present", "not_applicable"):
            quote, doc, page = None, None, None

    if name in YES_NO_LEGAL and status == "found" and norm is False and not quote:
        # "no" with nothing quoted is the model saying the clause is absent
        status, raw = "not_present", None
    if name == "card_type" and status == "found" and not quote:
        status = "not_present"  # no wording quoted: falls through to the default rule below
        raw = None
    if name == "card_type" and status == "found" and norm not in ("consumer", "business"):
        norm = "business" if re.search(r"business|commercial|corporate", f"{raw} {quote}", re.I) else "consumer"
    defaulted = False
    if name == "card_type" and status in ("not_present", "ambiguous") and raw in (None, ""):
        # audit rule: nothing stated -> consumer, status found, confidence medium. No quote exists.
        status, norm, conf, note = "found", "consumer", "medium", DEFAULT_NOTE
        raw, defaulted = DEFAULT_NOTE, True

    sha, file_name = doc_map.get(doc, (None, None))
    if defaulted:
        sha, file_name = next(iter(doc_map.values()))
        page, quote = 1, ""
    elif status in ("found", "stated_none") and (not quote or not sha or not page):
        # evidence missing: keep the value but mark it for the retry queue
        note = (note + "; " if note else "") + "missing quote, document or page"
        sha = sha or next(iter(doc_map.values()))[0]
        page = page or 1
        quote = quote or ""
    return {"status": status, "raw_value": raw if raw is None else str(raw), "normalized_value": norm,
            "quote": quote, "source_sha256": sha, "source_pdf": file_name, "page": page,
            "display": display_for(status, raw), "confidence": conf, "note": note}


def parse_date(s) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except ValueError:
            pass
    return None


_FILE_DATES = [
    (re.compile(r"(\d{1,2})[-.](\d{1,2})[-.](20\d{2})"), lambda m: (int(m[3]), int(m[1]), int(m[2]))),
    (re.compile(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(?<!\d)(\d{2})(\d{2})(2[0-9])(?!\d)"), lambda m: (2000 + int(m[3]), int(m[1]), int(m[2]))),
    (re.compile(r"(\d{1,2})[.\-](20\d{2})"), lambda m: (int(m[2]), int(m[1]), 1)),
]


def date_from_file_name(name: str) -> date | None:
    stem = re.sub(r"\.pdf-\d+\.pdf$", "", name, flags=re.IGNORECASE)
    for pat, parts in _FILE_DATES:
        m = pat.search(stem)
        if m:
            try:
                y, mo, d = parts(m)
                return date(y, mo, d)
            except ValueError:
                continue
    return None


def pdf_created_date(s: str | None) -> date | None:
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", s or "")
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def terms_as_of(d: date | None, source: str | None) -> str | None:
    if not d:
        return None
    words = {"effective_date": "date printed in the agreement", "file_name": "date in the file name",
             "pdf_created": "PDF creation date"}[source]
    return f"Terms as of {d.strftime('%B')} {d.day}, {d.year} ({words})"


def eligibility(fields: dict) -> tuple[bool, list[str]]:
    """comparison_eligible + reasons (audit prompt DATA SCOPE item 3)."""
    reasons = []
    ms = fields.get("market_scope", {})
    if ms.get("normalized_value") == "non_us":
        reasons.append("market_scope non_us")
    if ms.get("normalized_value") == "unclear" or ms.get("status") in ("ambiguous", "unreadable"):
        reasons.append("market_scope unclear")
    if fields.get("card_type", {}).get("normalized_value") == "business":
        reasons.append("business card")
    pa = fields.get("purchase_apr", {}).get("status")
    if pa in ("unreadable", "ambiguous"):
        reasons.append(f"purchase_apr {pa}")
    if all(fields.get(f, {}).get("status") == "not_present" for f in ("purchase_apr", "annual_fee", "late_fee")):
        reasons.append("no pricing information")
    if pa == "not_applicable" and not reasons:
        return True, ["charge card: no APR"]
    return not reasons, reasons
