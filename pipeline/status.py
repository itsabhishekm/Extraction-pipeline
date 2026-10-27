"""Print where every PDF is in the pipeline, and reconcile the counts."""
import json

from .config import Config
from .db import connect


def run(cfg: Config) -> dict:
    with connect(cfg.database_url) as conn:
        files = conn.execute(
            """SELECT count(*) AS files,
                      count(*) FILTER (WHERE is_duplicate) AS duplicate_files,
                      count(DISTINCT sha256) AS unique_documents,
                      count(DISTINCT issuer_id) AS issuers
               FROM document_files WHERE quarter = %s""", (cfg.quarter,)).fetchone()
        stages = conn.execute(
            """SELECT d.stage, d.status, count(*) AS documents,
                      coalesce(sum(d.page_count), 0) AS pages,
                      coalesce(sum(cardinality(d.ocr_pages)), 0) AS ocr_pages
               FROM documents d
               WHERE d.sha256 IN (SELECT sha256 FROM document_files WHERE quarter = %s)
               GROUP BY 1, 2 ORDER BY 1, 2""", (cfg.quarter,)).fetchall()
        failed = conn.execute(
            """SELECT i.folder_name, f.file_name, d.stage, d.last_error
               FROM documents d
               JOIN LATERAL (SELECT * FROM document_files x WHERE x.sha256 = d.sha256
                             ORDER BY file_id LIMIT 1) f ON true
               JOIN issuers i ON i.issuer_id = f.issuer_id
               WHERE d.status = 'failed' AND f.quarter = %s
               ORDER BY 1, 2""", (cfg.quarter,)).fetchall()

    print(f"\nQuarter {cfg.quarter}: {files['issuers']} issuers, {files['files']} files, "
          f"{files['unique_documents']} unique documents, {files['duplicate_files']} duplicate files\n")
    print(f"{'stage':<12}{'status':<8}{'documents':>10}{'pages':>8}{'ocr pages':>11}")
    for s in stages:
        print(f"{s['stage']:<12}{s['status']:<8}{s['documents']:>10}{s['pages']:>8}{s['ocr_pages']:>11}")
    total = sum(s["documents"] for s in stages)
    ok = "OK" if total == files["unique_documents"] else "MISMATCH"
    print(f"\nreconcile: {total} documents in stages vs {files['unique_documents']} unique -> {ok}")
    if failed:
        print(f"\n{len(failed)} failed:")
        for f in failed:
            print(f"  {f['folder_name']} / {f['file_name']} [{f['stage']}]: {f['last_error']}")

    with connect(cfg.database_url) as conn:
        versions = conn.execute(
            """SELECT stage, count(*) AS n, count(DISTINCT card_id) AS cards,
                      count(*) FILTER (WHERE needs_manual_review) AS review
               FROM card_versions WHERE quarter = %s GROUP BY 1 ORDER BY 1""", (cfg.quarter,)).fetchall()
        values = conn.execute(
            """SELECT fv.validation, count(*) AS n FROM field_values fv
               JOIN card_versions v USING (version_id) WHERE v.quarter = %s GROUP BY 1 ORDER BY 1""",
            (cfg.quarter,)).fetchall()
        statuses = conn.execute(
            """SELECT fv.status, count(*) AS n FROM field_values fv
               JOIN card_versions v USING (version_id) WHERE v.quarter = %s GROUP BY 1 ORDER BY 2 DESC""",
            (cfg.quarter,)).fetchall()
    if versions:
        print(f"\ncard versions: {sum(v['n'] for v in versions)} "
              f"({sum(v['cards'] for v in versions)} cards)")
        for v in versions:
            print(f"  {v['stage']:<11}{v['n']:>6}   needs manual review: {v['review']}")
    if values:
        print("field values by validation: " + ", ".join(f"{v['validation']} {v['n']}" for v in values))
        print("field values by status:     " + ", ".join(f"{s['status']} {s['n']}" for s in statuses))
        print(f"JSON files: {cfg.data_root / 'json'}")
        print(f"retry queue: {cfg.data_root / 'retry_queue.jsonl'}")
    usage = trace_usage(cfg)
    if usage:
        print("\nmodel calls (from the trace files; local models cost GPU time, not money):")
        print(f"  {'stage':<10}{'kind':<24}{'calls':>7}{'errors':>7}{'hours':>8}{'prompt tok':>12}{'output tok':>12}")
        for (stage, kind), u in sorted(usage.items()):
            print(f"  {stage or '-':<10}{kind:<24}{u['calls']:>7}{u['errors']:>7}{u['seconds'] / 3600:>8.2f}"
                  f"{u['prompt']:>12}{u['output']:>12}")
        print(f"  traces: {cfg.data_root / 'traces'}")
    return {"files": files, "stages": stages, "failed": failed, "usage": usage}


def trace_usage(cfg: Config) -> dict:
    out = {}
    for path in sorted((cfg.data_root / "traces").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = out.setdefault((r.get("stage"), r.get("kind", "?")),
                               {"calls": 0, "errors": 0, "seconds": 0.0, "prompt": 0, "output": 0})
            if "error" in r:
                u["errors"] += 1
                continue
            u["calls"] += 1
            u["seconds"] += r.get("seconds") or 0
            u["prompt"] += r.get("prompt_tokens") or 0
            u["output"] += r.get("output_tokens") or 0
    return out
