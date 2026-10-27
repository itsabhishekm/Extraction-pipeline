"""Offline evals: score the pipeline against hand-labelled cards, and the release gate.

    python run.py eval --make-template --count 150   # pre-filled answer-key files to check by hand
    python run.py eval                                # score + release gate (exit code 1 if it fails)
    python run.py eval --make-examples                # worked examples for the prompts (examples.yaml)

Answer key: one JSON file per card version in eval.answer_key_dir (default evals/answer_key/):
    {"issuer_folder": "...", "files": ["file names of the version"], "product_name": null,
     "checked": false, "labelled_by": "", "used_as_example": false,
     "fields": {"purchase_apr": {"status": "found", "raw_value": "...", "normalized_value": {...},
                                 "quote": "...", "source_pdf": "...", "page": 1}, ...}}
--make-template writes these files from the pipeline's own answers. A person opens each PDF,
corrects every field, and sets "checked": true. Only checked files are scored.
Files with "used_as_example": true feed the prompts (eval --make-examples), so they are NOT scored;
label them in addition to the eval cards.

A field is correct when the status matches and, for found / stated_none, the normalized value
matches (numbers to 2 decimals; null keys ignored; if the key has no normalized value the
raw values are compared after normalizing spaces and case).

Release gate (config `eval`): enough checked cards, overall accuracy, every field's accuracy,
and no field dropping more than `max_drop` below the last passing eval.
"""
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .config import HERE, Config, setup_logging
from .db import code_version, connect
from .pages import _MD, normalize
from .stage_extract import fields_by_name, load_fields, version_context
from .stage_validate import same_value


def key_dir(cfg) -> Path:
    d = Path(cfg.section("eval").get("answer_key_dir", "evals/answer_key"))
    return d if d.is_absolute() else HERE / d


def load_keys(cfg):
    out = []
    for p in sorted(key_dir(cfg).glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except json.JSONDecodeError as e:
            print(f"answer key {p.name} is not valid JSON: {e}", file=sys.stderr)
    return out


def version_files(conn, vid):
    return sorted(r["file_name"] for r in conn.execute(
        """SELECT DISTINCT f.file_name FROM version_documents vd JOIN card_versions v USING (version_id)
           JOIN document_files f ON f.sha256 = vd.sha256 AND f.quarter = v.quarter
           WHERE vd.version_id = %s""", (vid,)).fetchall())


def find_version(conn, cfg, key):
    """The version of this quarter made of the same files (and product) as the key."""
    want = sorted(key["files"])
    rows = conn.execute(
        """SELECT DISTINCT v.version_id, v.product_name FROM card_versions v
           JOIN version_documents vd USING (version_id)
           JOIN document_files f ON f.sha256 = vd.sha256 AND f.quarter = v.quarter
           JOIN issuers i ON i.issuer_id = f.issuer_id
           WHERE v.quarter = %s AND i.folder_name = %s AND f.file_name = ANY(%s)""",
        (cfg.quarter, key["issuer_folder"], want)).fetchall()
    for r in rows:
        if (r["product_name"] or None) == (key.get("product_name") or None) and version_files(conn, r["version_id"]) == want:
            return r["version_id"]
    return None


def pipeline_values(conn, vid):
    return {r["field_name"]: r for r in conn.execute(
        """SELECT fv.*, f.file_name AS source_pdf FROM field_values fv
           LEFT JOIN LATERAL (SELECT file_name FROM document_files x WHERE x.sha256 = fv.source_sha256
                              ORDER BY file_id LIMIT 1) f ON true WHERE fv.version_id = %s""", (vid,)).fetchall()}


def _text(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def field_correct(key_f, got) -> tuple[bool, bool]:
    """(status matches, fully correct)."""
    if got is None:
        return False, False
    if key_f["status"] != got["status"]:
        return False, False
    if key_f["status"] not in ("found", "stated_none"):
        return True, True
    kn = key_f.get("normalized_value")
    if kn is not None:
        return True, same_value(kn, got["normalized_value"])
    return True, _text(key_f.get("raw_value")) == _text(got["raw_value"])


# ----------------------------------------------------------------------------- scoring + gate
def score(cfg: Config) -> dict:
    log = setup_logging(cfg, "eval")
    spec = load_fields()
    fdefs = fields_by_name(spec)
    per_field = {}
    cards, missing, skipped = [], [], 0
    with connect(cfg.database_url) as conn:
        for path, key in load_keys(cfg):
            if not key.get("checked") or key.get("used_as_example"):
                skipped += 1
                continue
            vid = find_version(conn, cfg, key)
            if vid is None:
                missing.append(path.name)
                continue
            got = pipeline_values(conn, vid)
            wrong = []
            for fname, kf in (key.get("fields") or {}).items():
                if fname not in fdefs:
                    continue
                s_ok, ok = field_correct(kf, got.get(fname))
                pf = per_field.setdefault(fname, {"n": 0, "correct": 0, "status_correct": 0,
                                                  "key_found": 0, "found_correct": 0, "pipe_found": 0})
                pf["n"] += 1
                pf["correct"] += ok
                pf["status_correct"] += s_ok
                key_found = kf["status"] in ("found", "stated_none")
                pipe_found = got.get(fname) is not None and got[fname]["status"] in ("found", "stated_none")
                pf["key_found"] += key_found
                pf["pipe_found"] += pipe_found
                pf["found_correct"] += key_found and ok
                if not ok:
                    g = got.get(fname) or {}
                    wrong.append({"field": fname, "expected": {k: kf.get(k) for k in ("status", "normalized_value",
                                                                                      "raw_value")},
                                  "got": {"status": g.get("status"), "normalized_value": g.get("normalized_value"),
                                          "raw_value": g.get("raw_value"), "page": g.get("page_no")}})
            cards.append({"key_file": path.name, "version_id": vid, "wrong": wrong})
    for pf in per_field.values():
        pf["accuracy"] = round(pf["correct"] / pf["n"], 4) if pf["n"] else None
        pf["recall"] = round(pf["found_correct"] / pf["key_found"], 4) if pf["key_found"] else None
        pf["precision"] = round(pf["found_correct"] / pf["pipe_found"], 4) if pf["pipe_found"] else None
    n = sum(pf["n"] for pf in per_field.values())
    overall = round(sum(pf["correct"] for pf in per_field.values()) / n, 4) if n else 0.0
    groups = {}
    for fname, pf in per_field.items():
        g = groups.setdefault(fdefs[fname]["_group"], [0, 0])
        g[0] += pf["correct"]
        g[1] += pf["n"]
    result = {"quarter": cfg.quarter, "at": datetime.now().isoformat(timespec="seconds"),
              "code_version": code_version(), "prompt_version": spec["prompt_version"],
              "model": cfg.section("models").get("extract"), "cards_scored": len(cards),
              "keys_not_checked_or_examples": skipped, "keys_without_matching_version": missing,
              "overall_accuracy": overall,
              "group_accuracy": {g: round(c / t, 4) for g, (c, t) in groups.items() if t},
              "fields": dict(sorted(per_field.items())), "cards": cards}
    result["gate"] = release_gate(cfg, result)
    out_dir = cfg.data_root / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"eval_{cfg.quarter}_{stamp}.json").write_text(json.dumps(result, indent=2, default=str),
                                                              encoding="utf-8")
    (out_dir / f"eval_{cfg.quarter}_{stamp}.md").write_text(to_markdown(result), encoding="utf-8")
    with (out_dir / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: result[k] for k in ("quarter", "at", "code_version", "prompt_version", "model",
                                                   "cards_scored", "overall_accuracy")}
                           | {"passed": result["gate"]["passed"],
                              "fields": {k: v["accuracy"] for k, v in per_field.items()}}) + "\n")
    log.info("eval: %d cards, overall accuracy %.1f%%, release gate %s%s", len(cards), overall * 100,
             "PASSED" if result["gate"]["passed"] else "FAILED",
             "" if result["gate"]["passed"] else ": " + "; ".join(result["gate"]["failures"]))
    log.info("eval report: %s", out_dir / f"eval_{cfg.quarter}_{stamp}.md")
    return result


def last_passing(cfg):
    p = cfg.data_root / "evals" / "history.jsonl"
    if not p.exists():
        return None
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r.get("passed")]
    return rows[-1] if rows else None


def release_gate(cfg, result) -> dict:
    e = cfg.section("eval")
    min_cards, min_overall = e.get("min_cards", 150), e.get("min_overall", 0.90)
    min_field, min_n, max_drop = e.get("min_field", 0.80), e.get("min_field_n", 5), e.get("max_drop", 0.02)
    fails = []
    if result["cards_scored"] < min_cards:
        fails.append(f"only {result['cards_scored']} checked cards scored (need {min_cards})")
    if result["overall_accuracy"] < min_overall:
        fails.append(f"overall accuracy {result['overall_accuracy']:.1%} < {min_overall:.0%}")
    for f, pf in result["fields"].items():
        if pf["n"] >= min_n and pf["accuracy"] is not None and pf["accuracy"] < min_field:
            fails.append(f"{f} accuracy {pf['accuracy']:.1%} < {min_field:.0%}")
    prev = last_passing(cfg)
    if prev:
        for f, acc in (prev.get("fields") or {}).items():
            now = (result["fields"].get(f) or {}).get("accuracy")
            if acc is not None and now is not None and now < acc - max_drop:
                fails.append(f"{f} dropped from {acc:.1%} to {now:.1%} since the last passing eval ({prev['at']})")
    return {"passed": not fails, "failures": fails,
            "thresholds": {"min_cards": min_cards, "min_overall": min_overall, "min_field": min_field,
                           "max_drop": max_drop}, "compared_with": prev["at"] if prev else None}


def to_markdown(r) -> str:
    g = r["gate"]
    lines = [f"# Eval {r['quarter']} - {r['at']}", "",
             f"Release gate: **{'PASSED' if g['passed'] else 'FAILED'}**", ""]
    lines += [f"- {f}" for f in g["failures"]]
    lines += ["", f"Model {r['model']}, prompt {r['prompt_version']}, code {r['code_version']}. "
                  f"Cards scored: {r['cards_scored']}. Overall accuracy: {r['overall_accuracy']:.1%}.", "",
              "| Group | Accuracy |", "|---|---|"]
    lines += [f"| {k} | {v:.1%} |" for k, v in r["group_accuracy"].items()]
    lines += ["", "| Field | n | Accuracy | Status right | Recall (found) | Precision (found) |", "|---|---|---|---|---|---|"]
    pct = lambda x: "-" if x is None else f"{x:.1%}"  # noqa: E731
    for f, pf in r["fields"].items():
        lines.append(f"| {f} | {pf['n']} | {pct(pf['accuracy'])} | {pct(pf['status_correct'] / pf['n'])} | "
                     f"{pct(pf['recall'])} | {pct(pf['precision'])} |")
    if r["keys_without_matching_version"]:
        lines += ["", "Answer-key files with no matching card version: "
                  + ", ".join(r["keys_without_matching_version"])]
    lines += ["", "## Wrong values", ""]
    for c in r["cards"]:
        for w in c["wrong"]:
            lines.append(f"- {c['key_file']} `{w['field']}`: expected {json.dumps(w['expected'], default=str)} "
                         f"got {json.dumps(w['got'], default=str)}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- templates
def make_templates(cfg: Config, count: int = 150) -> int:
    """Pre-filled answer-key files for `count` latest versions, spread over issuers (round robin)."""
    d = key_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    existing = {(k["issuer_folder"], tuple(sorted(k["files"])), k.get("product_name")) for _, k in load_keys(cfg)}
    with connect(cfg.database_url) as conn:
        rows = conn.execute(
            """SELECT v.version_id, v.product_name, v.version_date, c.card_slug, i.folder_name, i.bank_slug
               FROM card_versions v JOIN cards c USING (card_id) JOIN issuers i USING (issuer_id)
               WHERE v.quarter=%s AND v.is_latest AND v.stage IN ('validated','embedded')
               ORDER BY i.folder_name, v.version_id""", (cfg.quarter,)).fetchall()
        by_issuer = {}
        for r in rows:
            by_issuer.setdefault(r["folder_name"], []).append(r)
        picked, queues = [], list(by_issuer.values())
        while len(picked) < count and any(queues):
            for q in queues:
                if q and len(picked) < count:
                    picked.append(q.pop(0))
        written = 0
        for r in picked:
            files = version_files(conn, r["version_id"])
            if (r["folder_name"], tuple(files), r["product_name"]) in existing:
                continue
            vals = pipeline_values(conn, r["version_id"])
            key = {"issuer_folder": r["folder_name"], "files": files, "product_name": r["product_name"],
                   "checked": False, "labelled_by": "", "used_as_example": False,
                   "note": "Pre-filled from the pipeline (version %d). Open each PDF, correct every field, then "
                           "set checked to true." % r["version_id"],
                   "fields": {f: {"status": v["status"], "raw_value": v["raw_value"],
                                  "normalized_value": v["normalized_value"], "quote": v["quote"],
                                  "source_pdf": v["source_pdf"], "page": v["page_no"]}
                              for f, v in sorted(vals.items()) if f != "submission_id"}}
            name = f"{r['bank_slug']}__{r['card_slug']}__{r['version_date'] or 'v' + str(r['version_id'])}.json"
            (d / name).write_text(json.dumps(key, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            written += 1
    print(f"{written} answer-key templates written to {d} ({len(picked)} picked over {len(by_issuer)} issuers)")
    return written


# ----------------------------------------------------------------------------- worked examples
def _window(text: str, quote: str, chars: int = 350) -> str:
    """The part of a page around a quote (markup removed, so it reads like the quote)."""
    flat = re.sub(r"\s+", " ", _MD.sub(" ", html.unescape(text or ""))).strip()
    q = normalize(quote)
    k = flat.lower().find(q[:60]) if q else -1
    if k < 0:
        return ""
    return flat[max(0, k - chars):k + len(q) + chars].strip()


def make_examples(cfg: Config, per_group: int = 2) -> Path:
    """examples.yaml from the checked answer-key files marked "used_as_example": true.
    Those files are never scored, so the eval stays honest (label them in addition to the 150)."""
    spec = load_fields()
    chosen = [(p, k) for p, k in load_keys(cfg) if k.get("checked") and k.get("used_as_example")]
    if not chosen:
        print('no answer-key files have "checked": true and "used_as_example": true. Pick 2-4 hand-checked cards '
              'of different layouts (not counted in the 150 eval cards), set both flags, and run this again.')
        return HERE / "examples.yaml"
    groups = {}
    with connect(cfg.database_url) as conn:
        for p, k in chosen:
            vid = find_version(conn, cfg, k)
            if vid is None:
                continue
            _, pages = version_context(conn, vid)
            labels = {}
            for gname, gdef in spec["groups"].items():
                if len(groups.get(gname, [])) >= per_group:
                    continue
                text_parts, answer = [], {}
                for fname in gdef["fields"]:
                    kf = (k.get("fields") or {}).get(fname)
                    if not kf:
                        continue
                    ans = {x: kf.get(x) for x in ("status", "raw_value", "normalized_value", "quote")}
                    ans.update(doc=None, page=None, confidence="high")
                    if kf["status"] in ("found", "stated_none") and kf.get("quote"):
                        page = next((pg for pg in pages if pg.file_name == kf.get("source_pdf")
                                     and pg.page_no == kf.get("page")), None)
                        win = _window(page.text, kf["quote"]) if page else ""
                        if not win:
                            continue
                        label = labels.setdefault(kf["source_pdf"], f"D{len(labels) + 1}")
                        text_parts.append(f"=== [{label} page {kf['page']}] ===\n...{win}...")
                        ans.update(doc=label, page=kf["page"])
                        answer[fname] = ans
                if text_parts and answer:
                    groups.setdefault(gname, []).append({"text": "\n".join(dict.fromkeys(text_parts)),
                                                         "answer": answer, "from": p.name})
    out = HERE / "examples.yaml"
    header = ("# Worked examples for the extraction prompts, built by `python run.py eval --make-examples`\n"
              "# from the checked answer-key files marked used_as_example (they are not scored by the eval).\n"
              "# Changing this file changes the code_version recorded with every value.\n\n")
    out.write_text(header + yaml.safe_dump({"groups": groups}, allow_unicode=True, sort_keys=False, width=120),
                   encoding="utf-8")
    print("examples.yaml: " + ", ".join(f"{g}: {len(v)}" for g, v in groups.items()) if groups
          else "no examples could be built (need checked answer keys with quotes found in the text)")
    return out


def run(cfg: Config, make_template=False, count=150, make_examples_=False) -> int:
    if make_template:
        make_templates(cfg, count)
        return 0
    if make_examples_:
        make_examples(cfg)
        return 0
    return 0 if score(cfg)["gate"]["passed"] else 1
