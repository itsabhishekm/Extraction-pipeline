"""Checks of the Validation Gate rules, chunking and eval scoring that need no database or model.

    python -m tests.test_gate
"""
from types import SimpleNamespace

from pipeline import embed, evals, stage_validate as sv
from pipeline.pages import Page


def page(text, no=1, ocr=False, doc="D1", sha="a" * 64):
    return Page(doc=doc, sha256=sha, file_name="x.pdf", role="pricing", page_no=no, text=text, ocr=ocr)


def test_quote():
    t = "| **Annual Fee** | None |\n\nLate Payment Fee  Up to $41"
    assert sv.quote_check("Annual Fee None", t, False)[0]
    assert sv.quote_check("Late Payment Fee Up to $41", t, False)[0]
    assert not sv.quote_check("Late Payment Fee Up to $99 and much more text here", t, False)[0]


def test_schema_and_ranges():
    apr = {"type": "object", "properties": {"min": {"type": ["number", "null"]}, "max": {"type": ["number", "null"]}}}
    fdef = {"normalized": apr}
    assert not sv.schema_check({"normalized_value": {"min": 18.49, "max": 29.99}}, fdef)
    assert sv.schema_check({"normalized_value": {"min": "18.49%"}}, fdef)
    assert sv.schema_check({"normalized_value": "SD1"}, {"normalized": {"type": "string", "pattern": "^[A-Z]{2}$"}})
    r = sv.DEFAULT_RANGES
    assert not sv.range_check({"field_name": "purchase_apr", "normalized_value": {"min": 18.49, "max": 29.99}}, r)
    assert sv.range_check({"field_name": "purchase_apr", "normalized_value": {"min": 1849}}, r)
    assert sv.range_check({"field_name": "late_fee", "normalized_value": {"first_usd": 410}}, r)
    assert sv.range_check({"field_name": "payment_due_days", "normalized_value": 14}, r)


def test_numbers():
    fv = {"field_name": "late_fee", "status": "found", "raw_value": "Up to $41", "quote": "Late fee up to $40",
          "normalized_value": None, "source_sha256": "a" * 64, "page_no": 1}
    errs, _, _ = sv.code_checks(fv, {"a" * 64: {1: page("Late fee up to $40")}}, {"normalized": {"type": "null"},
                                                                                  "_group": "pricing"}, {})
    assert any("numbers" in e for e in errs)


def test_page_correction():
    fv = {"field_name": "annual_fee", "status": "found", "raw_value": "None", "quote": "Annual Fee None",
          "normalized_value": None, "source_sha256": "a" * 64, "page_no": 1}
    pages = {"a" * 64: {1: page("nothing here"), 2: page("Annual Fee | None", no=2)}}
    errs, notes, new = sv.code_checks(fv, pages, {"normalized": {"type": "null"}, "_group": "pricing"}, {})
    assert not errs and new == 2


def test_dpr():
    rows = {"purchase_apr": {"normalized_value": {"min": 18.25, "max": 18.25}}}
    ok = [page("Daily Periodic Rate of 0.05% (corresponding ANNUAL PERCENTAGE RATE of 18.25%)")]
    bad = [page("daily periodic rate 0.0800% ANNUAL PERCENTAGE RATE 18.25%")]
    assert not sv.dpr_check(ok, rows)
    assert sv.dpr_check(bad, rows)


def test_same_value():
    assert sv.same_value({"min": 18.49, "max": None}, {"min": 18.490001, "max": 29.99})
    assert not sv.same_value({"min": 18.49}, {"min": 19.49})
    assert sv.same_value(True, True) and not sv.same_value(True, False)
    assert not sv.same_value(1, True)


def test_cross_field():
    executed = []
    conn = SimpleNamespace(execute=lambda *a: executed.append(a))
    rows = {"arbitration_clause": {"status": "not_present", "normalized_value": None},
            "arbitration_opt_out": {"status": "found", "normalized_value": {"allowed": True}, "value_id": 7,
                                    "note": None},
            "military_exception": {"status": "found", "normalized_value": True}}
    errs = sv.cross_field_rules(conn, rows)
    assert rows["arbitration_opt_out"]["status"] == "not_applicable" and executed
    assert "military_exception" in errs
    rows = {"arbitration_clause": {"status": "found", "normalized_value": True},
            "arbitration_opt_out": {"status": "not_present", "normalized_value": None, "value_id": 8, "note": None}}
    assert "arbitration_opt_out" in sv.cross_field_rules(conn, rows)


def test_double_read_conflict():
    """Two PDFs of one version give different late fees -> a conflicts row + human review."""
    calls = []
    answers = {"D1": "41", "D2": "35"}

    def fake_extract(model, gdef, fields, docs, pages, **kw):
        d = docs[0]["label"]
        return {"late_fee": {"status": "found", "raw_value": f"${answers[d]}",
                             "normalized_value": {"first_usd": float(answers[d])}, "quote": f"Late fee ${answers[d]}",
                             "doc": d, "page": 1, "confidence": "high"}}, {}

    orig = sv.extract_group
    sv.extract_group = fake_extract
    try:
        conn = SimpleNamespace(execute=lambda *a: calls.append(a))
        cfg = SimpleNamespace(section=lambda n: {})
        spec = {"groups": {"pricing": {"keywords": ["late"], "fields": {"late_fee": {"normalized": {"type": "object"}}}}}}
        docs = [{"label": "D1", "sha256": "a" * 64, "file_name": "a.pdf"},
                {"label": "D2", "sha256": "b" * 64, "file_name": "b.pdf"}]
        pages = [page("Late fee $41", doc="D1", sha="a" * 64), page("Late fee $35", doc="D2", sha="b" * 64)]
        rows = {"late_fee": {"status": "found", "normalized_value": {"first_usd": 41.0}, "source_sha256": "a" * 64,
                             "note": None}}
        retry, human = sv.double_read(conn, cfg, "m", spec, {"version_id": 1}, docs, pages, rows, {})
    finally:
        sv.extract_group = orig
    assert "late_fee" in human and any("INSERT INTO conflicts" in c[0] for c in calls), (retry, human)


def test_chunks():
    text = "# Fees\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))
    chunks = embed.chunk_page(text, 1200, 200)
    assert all(len(t) <= 1200 + 200 + 400 for _, _, t in chunks) and len(chunks) > 3
    assert chunks[0][1] == "Fees"
    d, s = embed.FakeEmbedder().encode(["late fee", "annual fee"])
    assert len(d[0]) == embed.DIM and s[0]


def test_eval_scoring():
    got = {"status": "found", "normalized_value": {"first_usd": 41}, "raw_value": "Up to $41"}
    assert evals.field_correct({"status": "found", "normalized_value": {"first_usd": 41.0}}, got) == (True, True)
    assert evals.field_correct({"status": "found", "normalized_value": {"first_usd": 40}}, got) == (True, False)
    assert evals.field_correct({"status": "not_present"}, got) == (False, False)


def test_llm_fallback():
    """Thinking runs out of tokens -> asked again with thinking off; a looping answer -> retried."""
    from pipeline import llm
    calls = []

    def chat(model, messages, format=None, think=None, options=None, **_):
        calls.append((think, options))
        if think:  # thinking never finishes
            return SimpleNamespace(message=SimpleNamespace(content=""), done_reason="length",
                                   prompt_eval_count=3000, eval_count=options["num_predict"])
        if len(calls) == 2:  # first non-thinking try loops and is cut off mid-JSON
            return SimpleNamespace(message=SimpleNamespace(content='{"a": "aaaa'), done_reason="length",
                                   prompt_eval_count=3000, eval_count=options["num_predict"])
        return SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'), done_reason="stop",
                               prompt_eval_count=3000, eval_count=5)

    old = llm._client
    llm._client = SimpleNamespace(chat=chat)
    try:
        data, stats = llm.extract_json("m", "s", "u", {"type": "object"}, think=True, retries=1)
    finally:
        llm._client = old
    assert data == {"a": 1} and stats["attempt"] == 3 and not stats["think"]
    assert calls[0][1]["num_predict"] == llm._limits["think_max_tokens"]
    assert calls[1][0] is False and calls[1][1]["temperature"] == 0 and calls[1][1]["repeat_penalty"] > 1


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} checks passed")
