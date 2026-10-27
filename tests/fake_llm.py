"""Stand-in for the `ollama` package, for testing the pipeline without a GPU (CC_FAKE_LLM=1).

It is NOT a model: it answers each field by finding the first line that contains a keyword
and quoting it, so the output has the right shape and real quotes. On the first try it
gives `minimum_interest_charge` a broken quote, so the validate -> retry path gets exercised.
"""
import json
import re
from types import SimpleNamespace

KEYWORDS = {
    "card_name": ["agreement", "card"], "issuer_name_in_pdf": ["bank", "credit union", "n.a."],
    "charter_hint": ["n.a.", "national association", "federal credit union"],
    "effective_date": ["effective", "as of"], "card_type": ["business", "commercial"],
    "market_scope": ["resident"], "secured": ["security deposit", "secured"],
    "issuer_contact": ["call", "www.", "p.o. box"],
    "purchase_apr": ["for purchases"], "balance_transfer_apr": ["balance transfer"],
    "balance_transfer_fee": ["balance transfer"], "cash_advance_apr": ["cash advance"],
    "cash_advance_fee": ["cash advance"], "late_fee": ["late payment"], "returned_payment_fee": ["returned payment"],
    "other_penalty_fees": ["over-the-credit-limit", "over the credit limit"], "penalty_apr": ["penalty apr"],
    "payment_due_days": ["days after the close"], "interest_free_grace_period": ["paying interest", "due date"],
    "annual_fee": ["annual fee"], "foreign_transaction_fee": ["foreign transaction"],
    "minimum_interest_charge": ["minimum interest charge"], "late_fee_grace_days": ["late fee", "late payment"],
    "arbitration_clause": ["arbitration"], "class_action_waiver": ["class action"], "jury_trial_waiver": ["jury"],
    "arbitration_opt_out": ["reject"], "military_exception": ["military lending act", "covered borrower"],
    "small_claims_carveout": ["small claims"], "governing_law_state": ["governed by", "law of"],
}
BOOLS = {"arbitration_clause", "class_action_waiver", "military_exception", "small_claims_carveout", "secured"}
MD = re.compile(r"\*\*|__|<br\s*/?>|</?sup>|\|")
PRODUCT = re.compile(r"((?:[A-Z][A-Za-z0-9]+(?:®)?\s){1,3}(?:Visa|Mastercard)(?:\s(?:Signature|Card))?)")
calls = {"ocr": 0, "extract": 0, "check": 0, "products": 0, "vision": 0}


def _pages(text):
    parts = re.split(r"^=== \[(D\d+) page (\d+)\] ===$", text, flags=re.MULTILINE)
    for i in range(1, len(parts) - 2, 3):
        yield parts[i], int(parts[i + 1]), parts[i + 2]


def _answer(field, text, retry):
    for doc, page, body in _pages(text):
        for line in body.splitlines():
            low = line.lower()
            if any(k in low for k in KEYWORDS.get(field, [])):
                quote = re.sub(r"\s+", " ", MD.sub(" ", line)).strip()[:300]
                if len(quote) < 8:
                    continue
                nums = re.findall(r"\$?\d+(?:\.\d+)?%?", quote)
                raw = nums[0] if nums and field not in BOOLS and field != "card_name" else quote[:80]
                if field == "minimum_interest_charge" and not retry:
                    quote = quote + " (this sentence is not in the document)"
                norm = True if field in BOOLS else None
                return {"status": "found", "raw_value": raw, "normalized_value": norm, "quote": quote,
                        "doc": doc, "page": page, "confidence": "medium"}
    return {"status": "not_present", "raw_value": None, "normalized_value": None, "quote": None,
            "doc": None, "page": None, "confidence": "high"}


def chat(model, messages, format=None, think=None, options=None, **_):
    if len(messages) == 1 and messages[0]["content"] == "Text Recognition:":
        calls["ocr"] += 1
        content = "FAKE OCR TEXT\nLate Payment Fee: Up to $40\nAnnual Fee: None"
        return SimpleNamespace(message=SimpleNamespace(content=content), prompt_eval_count=900, eval_count=30)
    system, user = messages[0]["content"], messages[1]["content"]
    if messages[1].get("images"):
        calls["vision"] += 1
    props = (format or {}).get("properties", {})
    if "products" in props:
        # products named on lines that also carry a percentage (rows of a pricing table)
        calls["products"] += 1
        names = []
        for doc, page, body in _pages(user.rsplit("\nTEXT:\n", 1)[-1]):
            for line in body.splitlines():
                if "%" not in line:
                    continue
                for m in PRODUCT.finditer(MD.sub(" ", line).replace("  ", " ")):
                    n = re.sub(r"\s+", " ", m.group(1)).strip()
                    if n not in [x["name"] for x in names] and len(names) < 4:
                        names.append({"name": n, "quote": n, "doc": doc, "page": page})
        out = {"products": names}
    elif system.startswith("You check"):
        calls["check"] += 1
        out = {k: {"supported": True, "reason": "fake check"} for k in props}
    elif "mentions" in props:
        calls["extract"] += 1
        out = {"mentions": []}
        for doc, page, body in _pages(user):
            for code, kw in [("MLA", "military lending act"), ("FCBA", "billing rights"), ("FAA", "federal arbitration act")]:
                for line in body.splitlines():
                    if kw in line.lower():
                        out["mentions"].append({"reg_code": code, "mention_type": "cited_by_name", "state": None,
                                                "quote": MD.sub(" ", line).strip()[:300], "doc": doc, "page": page,
                                                "confidence": "medium"})
                        break
    else:
        calls["extract"] += 1
        retry = "PREVIOUS ANSWER WAS REJECTED" in user
        text = user.rsplit("\nTEXT:\n", 1)[-1]
        out = {f: _answer(f, text, retry) for f in props}
    return SimpleNamespace(message=SimpleNamespace(content=json.dumps(out)), prompt_eval_count=len(user) // 4,
                           eval_count=len(json.dumps(out)) // 4)


def generate(model, prompt="", keep_alive=None, **_):
    return SimpleNamespace(response="")
