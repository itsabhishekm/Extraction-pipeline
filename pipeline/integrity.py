"""Integrity Check, run before publishing: counts match + test searches pass.

    python run.py integrity

Checks (each one PASS / FAIL, with the items that failed):
  files_recorded      every PDF under input_root is in document_files for this quarter, and back
  documents_done      no document is still waiting (scanned / needs_ocr) or failed without a reason
  md_pages            each .md file has one page marker per PDF page (= page_count)
  documents_grouped   every readable document belongs to a card version
  fields_complete     every extracted version has all target fields, each with a valid status
  evidence            every found / stated_none value has a quote, a PDF of its own version and a page
                      that exists; display matches status
  validation_done     no value is still pending / retry_queued; human-review items are decided
                      (integrity.allow_open_review: true lets open review items through)
  json_files          every card has its JSON file, it parses, and its versions / fields match Postgres
  qdrant_points       Qdrant holds exactly the expected chunks for the embedded versions
  test_searches       each query in search_tests.yaml finds its expected PDF in the top N
Writes <data_root>/integrity/integrity_<quarter>_<time>.json. Exit code 1 if any check fails.
"""
import json
from datetime import datetime
from pathlib import Path

import yaml

from .config import HERE, Config, setup_logging
from .db import connect
from .postprocess import display_for

PAGE_MARK = "<!-- page "


def md_page_count(md_path) -> int | None:
    p = Path(md_path or "")
    if not md_path or not p.exists():
        return None
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.startswith(PAGE_MARK))


def _check(name, problems, limit=50):
    return {"check": name, "passed": not problems, "problems": problems[:limit], "problem_count": len(problems)}


def bank_self_check(conn, cfg, issuer_id) -> list[str]:
    """The audit prompt's SELF-CHECK for one bank. Returns what failed (empty = passed)."""
    fails = []
    n_fields = conn.execute("SELECT count(*) AS n FROM field_definitions").fetchone()["n"]
    for v in conn.execute(
            """SELECT v.version_id, v.is_latest, v.market_scope, v.comparison_eligible, v.terms_as_of, v.json_path,
                      v.card_id, c.card_name, (SELECT count(*) FROM field_values f WHERE f.version_id=v.version_id) AS n
               FROM card_versions v JOIN cards c USING (card_id)
               WHERE c.issuer_id=%s AND v.quarter=%s AND v.stage IN ('validated','embedded')""",
            (issuer_id, cfg.quarter)).fetchall():
        tag = f"{v['card_name']} (version {v['version_id']})"
        if v["is_latest"] and v["n"] < n_fields:
            fails.append(f"1. {tag}: {v['n']} of {n_fields} fields")
        if v["market_scope"] in ("non_us", "unclear") and v["comparison_eligible"]:
            fails.append(f"8. {tag}: market_scope {v['market_scope']} but comparison_eligible true")
        if v["is_latest"] and not v["terms_as_of"]:
            fails.append(f"8. {tag}: terms_as_of missing")
        for fv in conn.execute("""SELECT fv.*, d.page_count FROM field_values fv
                                  LEFT JOIN documents d ON d.sha256 = fv.source_sha256
                                  WHERE fv.version_id=%s""", (v["version_id"],)).fetchall():
            if fv["validation"] not in ("passed", "human_approved"):
                fails.append(f"3. {tag} {fv['field_name']}: validation {fv['validation']}")
            if fv["status"] in ("found", "stated_none") and fv["page_count"] and fv["page_no"] > fv["page_count"]:
                fails.append(f"4. {tag} {fv['field_name']}: page {fv['page_no']} > {fv['page_count']} pages")
            if fv["field_name"] != "submission_id" and fv["display"] != display_for(fv["status"], fv["raw_value"]):
                fails.append(f"7. {tag} {fv['field_name']}: display does not match status")
        if v["json_path"] and not Path(v["json_path"]).exists():
            fails.append(f"5. {tag}: JSON file missing")
    slugs = conn.execute("SELECT card_slug, count(*) AS n FROM cards WHERE issuer_id=%s GROUP BY 1 HAVING count(*)>1",
                         (issuer_id,)).fetchall()
    fails += [f"6. two cards named {s['card_slug']}" for s in slugs]
    return fails


def run(cfg: Config) -> bool:
    log = setup_logging(cfg, "integrity")
    ic = cfg.section("integrity")
    results = []
    with connect(cfg.database_url) as conn:
        # files_recorded
        # the same rule as the scan stage: one level of issuer folders, names starting with skip_prefix skipped
        skip = cfg.raw.get("skip_prefix", "_")
        on_disk = {(d.name, f.name) for d in cfg.input_root.iterdir() if d.is_dir() and not d.name.startswith(skip)
                   for f in d.iterdir() if f.is_file() and not f.name.startswith(skip)
                   and f.suffix.lower() == ".pdf"}
        in_db = {(r["folder_name"], r["file_name"]) for r in conn.execute(
            """SELECT i.folder_name, f.file_name FROM document_files f JOIN issuers i USING (issuer_id)
               WHERE f.quarter=%s""", (cfg.quarter,)).fetchall()}
        results.append(_check("files_recorded",
                              [f"not scanned: {a}/{b}" for a, b in sorted(on_disk - in_db)]
                              + [f"in database but not on disk: {a}/{b}" for a, b in sorted(in_db - on_disk)]))

        docs = conn.execute(
            """SELECT d.*, f.file_name, i.folder_name FROM documents d
               JOIN LATERAL (SELECT * FROM document_files x WHERE x.sha256 = d.sha256 AND x.quarter=%s
                             ORDER BY file_id LIMIT 1) f ON true JOIN issuers i ON i.issuer_id = f.issuer_id""",
            (cfg.quarter,)).fetchall()
        results.append(_check("documents_done",
                              [f"{d['folder_name']}/{d['file_name']}: stage {d['stage']}" for d in docs
                               if d["status"] == "ok" and d["stage"] in ("scanned", "needs_ocr")]
                              + [f"{d['folder_name']}/{d['file_name']}: failed with no reason" for d in docs
                                 if d["status"] == "failed" and not d["last_error"]]))
        md = []
        for d in docs:
            if d["status"] != "ok":
                continue
            n = md_page_count(d["md_path"])
            if n is None:
                md.append(f"{d['folder_name']}/{d['file_name']}: .md file missing")
            elif n != d["page_count"]:
                md.append(f"{d['folder_name']}/{d['file_name']}: .md has {n} pages, PDF has {d['page_count']}")
        results.append(_check("md_pages", md))
        grouped = {r["sha256"] for r in conn.execute(
            "SELECT DISTINCT vd.sha256 FROM version_documents vd JOIN card_versions v USING (version_id) "
            "WHERE v.quarter=%s", (cfg.quarter,)).fetchall()}
        results.append(_check("documents_grouped", [f"{d['folder_name']}/{d['file_name']}: in no card version"
                                                    for d in docs if d["status"] == "ok" and d["sha256"] not in grouped]))

        n_fields = conn.execute("SELECT count(*) AS n FROM field_definitions").fetchone()["n"]
        versions = conn.execute(
            """SELECT v.*, c.card_name, i.folder_name,
                      (SELECT count(*) FROM field_values f WHERE f.version_id=v.version_id) AS n_values
               FROM card_versions v JOIN cards c USING (card_id) JOIN issuers i USING (issuer_id)
               WHERE v.quarter=%s""", (cfg.quarter,)).fetchall()
        results.append(_check("fields_complete", [
            f"v{v['version_id']} {v['folder_name']} / {v['card_name']}: stage {v['stage']}, {v['n_values']} of {n_fields} fields"
            for v in versions if v["stage"] == "grouped" or v["n_values"] != n_fields]))

        ev = conn.execute(
            """SELECT fv.value_id, fv.version_id, fv.field_name, fv.status, fv.raw_value, fv.display, fv.page_no,
                      fv.quote, fv.source_sha256, d.page_count,
                      EXISTS (SELECT 1 FROM version_documents vd WHERE vd.version_id = fv.version_id
                              AND vd.sha256 = fv.source_sha256) AS in_version
               FROM field_values fv JOIN card_versions v USING (version_id)
               LEFT JOIN documents d ON d.sha256 = fv.source_sha256 WHERE v.quarter=%s""", (cfg.quarter,)).fetchall()
        prob = []
        for r in ev:
            t = f"v{r['version_id']} {r['field_name']}"
            if r["status"] in ("found", "stated_none"):
                if not r["quote"] and r["field_name"] != "card_type":
                    prob.append(f"{t}: no quote")
                if not r["in_version"]:
                    prob.append(f"{t}: source PDF is not part of the version")
                if r["page_count"] and not 1 <= r["page_no"] <= r["page_count"]:
                    prob.append(f"{t}: page {r['page_no']} outside 1-{r['page_count']}")
            if r["field_name"] != "submission_id" and r["display"] != display_for(r["status"], r["raw_value"]):
                prob.append(f"{t}: display {r['display']!r} does not match status {r['status']}")
        results.append(_check("evidence", prob))

        val = conn.execute(
            """SELECT fv.validation, count(*) AS n FROM field_values fv JOIN card_versions v USING (version_id)
               WHERE v.quarter=%s AND fv.validation IN ('pending','retry_queued','failed','human_review')
               GROUP BY 1""", (cfg.quarter,)).fetchall()
        open_review = conn.execute(
            """SELECT count(*) AS n FROM review_items r JOIN field_values fv USING (value_id)
               JOIN card_versions v USING (version_id) WHERE v.quarter=%s AND r.decision IS NULL""",
            (cfg.quarter,)).fetchone()["n"]
        prob = [f"{r['n']} values {r['validation']}" for r in val
                if r["validation"] != "human_review" or not ic.get("allow_open_review", False)]
        if open_review and not ic.get("allow_open_review", False):
            prob.append(f"{open_review} human-review items not decided (python run.py review)")
        results.append(_check("validation_done", prob))

        prob = []
        for card in conn.execute(
                """SELECT c.card_id, c.card_name, max(v.json_path) AS json_path, count(*) AS n_versions
                   FROM cards c JOIN card_versions v USING (card_id) WHERE v.quarter=%s GROUP BY 1, 2""",
                (cfg.quarter,)).fetchall():
            p = Path(card["json_path"] or "")
            if not card["json_path"] or not p.exists():
                prob.append(f"{card['card_name']}: JSON file missing")
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                prob.append(f"{p.name}: not valid JSON ({e})")
                continue
            total_versions = conn.execute("SELECT count(*) AS n FROM card_versions WHERE card_id=%s",
                                          (card["card_id"],)).fetchone()["n"]
            if len(data.get("versions", [])) != total_versions:
                prob.append(f"{p.name}: {len(data.get('versions', []))} versions in JSON, {total_versions} in Postgres")
            for jv in data.get("versions", []):
                if jv.get("audit_level") == "full" and len(jv.get("fields", {})) != n_fields:
                    prob.append(f"{p.name}: a version has {len(jv['fields'])} fields")
                cov = sum(len(x) for x in (jv.get("coverage") or {}).values())
                if cov != len(jv.get("fields", {})):
                    prob.append(f"{p.name}: coverage lists {cov} fields, fields has {len(jv.get('fields', {}))}")
        results.append(_check("json_files", prob))

        # qdrant + test searches
        try:
            from . import embed
            client = embed.qdrant(cfg)
            name = embed.collection_name(cfg)
            want = embed.expected_chunks(cfg, conn)
            from qdrant_client import models as m
            have = client.count(name, count_filter=m.Filter(must=[m.FieldCondition(
                key="quarter", match=m.MatchValue(value=cfg.quarter))]), exact=True).count \
                if client.collection_exists(name) else 0
            results.append(_check("qdrant_points", [] if want == have else
                                  [f"Qdrant has {have} points for {cfg.quarter}, expected {want}"]))
            results.append(test_searches(cfg, client))
        except Exception as e:  # noqa: BLE001
            results.append(_check("qdrant_points", [f"could not check Qdrant: {e}"]))

    passed = all(r["passed"] for r in results)
    out = cfg.data_root / "integrity"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"integrity_{cfg.quarter}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"quarter": cfg.quarter, "passed": passed, "checks": results}, indent=2,
                               ensure_ascii=False), encoding="utf-8")
    for r in results:
        log.info("%-18s %s%s", r["check"], "PASS" if r["passed"] else "FAIL",
                 "" if r["passed"] else f" ({r['problem_count']}): " + "; ".join(r["problems"][:5]))
    log.info("integrity %s -> %s", "PASSED" if passed else "FAILED", path)
    return passed


def test_searches(cfg, client):
    from . import embed
    p = Path(cfg.section("integrity").get("search_tests", HERE / "search_tests.yaml"))
    if not p.is_absolute():
        p = HERE / p
    tests = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("tests") or [] if p.exists() else []
    prob = []
    ran = 0
    for t in tests:
        if t.get("quarter") and str(t["quarter"]) != cfg.quarter:
            continue
        top = t.get("top", 3)
        hits = embed.search(cfg, t["query"], top=top, issuer=t.get("issuer"), client=client)
        exp = t.get("expect", {})
        ok = any((not exp.get("issuer") or h["issuer"] == exp["issuer"])
                 and (not exp.get("file_contains") or exp["file_contains"].lower() in (h["file_name"] or "").lower())
                 and (not exp.get("text_contains") or exp["text_contains"].lower() in (h["text"] or "").lower())
                 for h in hits)
        ran += 1
        if not ok:
            prob.append(f"{t['query']!r}: expected {exp} in the top {top}, got "
                        + "; ".join(f"{h['issuer']}/{h['file_name']} p{h['page']}" for h in hits))
    if not ran:
        prob.append(f"no test searches for {cfg.quarter} in {p}")
    return _check("test_searches", prob)
