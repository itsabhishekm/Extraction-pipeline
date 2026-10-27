"""Publish a release: only when the Integrity Check AND the offline-eval release gate pass.

    python run.py publish

Copies the card JSON files, bank inventory files and the quarter summary to
<data_root>/releases/<quarter>_<time>/ with a manifest.json (counts, eval scores, model,
prompt and code versions, sha256 of every file). Nothing is copied if a check fails.
"""
import hashlib
import json
import shutil
from datetime import datetime

from . import evals, integrity, report
from .config import Config, setup_logging
from .db import code_version, connect, finish_run, start_run
from .stage_extract import load_fields


def run(cfg: Config, skip_eval: bool = False) -> bool:
    log = setup_logging(cfg, "publish")
    report.run(cfg)
    ok_integrity = integrity.run(cfg)
    log = setup_logging(cfg, "publish")
    ev = None
    if skip_eval:
        log.warning("eval release gate SKIPPED (--skip-eval): this release is not eval-approved")
    else:
        ev = evals.score(cfg)
        log = setup_logging(cfg, "publish")
    ok_eval = skip_eval or ev["gate"]["passed"]
    if not (ok_integrity and ok_eval):
        log.error("NOT published: integrity %s, eval gate %s", "passed" if ok_integrity else "FAILED",
                  "skipped" if skip_eval else "passed" if ok_eval else "FAILED")
        return False
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = cfg.data_root / "releases" / f"{cfg.quarter}_{stamp}"
    files = []
    for src in sorted((cfg.data_root / "json").rglob("*.json")):
        rel = src.relative_to(cfg.data_root / "json")
        (dest / "json" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / "json" / rel)
        files.append(("json/" + rel.as_posix(), src))
    summary = cfg.data_root / "reports" / f"{cfg.quarter}_summary.md"
    if summary.exists():
        shutil.copy2(summary, dest / summary.name)
        files.append((summary.name, summary))
    with connect(cfg.database_url) as conn:
        run_id = start_run(conn, cfg, "publish", load_fields()["prompt_version"])
        counts = conn.execute(
            """SELECT count(DISTINCT card_id) AS cards, count(*) AS versions FROM card_versions WHERE quarter=%s""",
            (cfg.quarter,)).fetchone()
        finish_run(conn, run_id)
    manifest = {"quarter": cfg.quarter, "published_at": stamp, "run_id": run_id, "code_version": code_version(),
                "prompt_version": load_fields()["prompt_version"], "models": cfg.section("models"),
                "cards": counts["cards"], "versions": counts["versions"],
                "eval": None if ev is None else {"overall_accuracy": ev["overall_accuracy"],
                                                 "cards_scored": ev["cards_scored"]},
                "eval_gate": "skipped" if skip_eval else "passed",
                "files": {name: hashlib.sha256(p.read_bytes()).hexdigest() for name, p in files}}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    log.info("published %d files -> %s", len(files), dest)
    return True
