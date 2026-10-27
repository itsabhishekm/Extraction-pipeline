"""Postgres connection, plus run bookkeeping for data lineage."""
import hashlib
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent.parent


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    conn.execute("SET search_path = cc, public")
    return conn


def code_version() -> str:
    """Fingerprint of the pipeline code + fields.yaml + examples.yaml (changes whenever any of them change)."""
    h = hashlib.sha256()
    files = sorted((HERE / "pipeline").glob("*.py")) + [HERE / "run.py", HERE / "fields.yaml", HERE / "examples.yaml"]
    for f in files:
        if f.exists():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return "code-" + h.hexdigest()[:12]


def start_run(conn, cfg, stage: str, prompt_version: str = "-") -> int:
    models = cfg.section("models")
    run_id = conn.execute(
        """INSERT INTO pipeline_runs (quarter, ocr_model, extraction_model, prompt_version, code_version, notes)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING run_id""",
        (cfg.quarter, models.get("ocr"), models.get("extract", "-"), prompt_version, code_version(), stage),
    ).fetchone()["run_id"]
    conn.commit()
    return run_id


def finish_run(conn, run_id: int) -> None:
    conn.execute("UPDATE pipeline_runs SET finished_at = now() WHERE run_id = %s", (run_id,))
    conn.commit()
