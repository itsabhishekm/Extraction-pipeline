"""Stage 1 - scan: find every PDF, hash it, and record it in the manifest.

One row per file in document_files, one row per unique content (sha256) in
documents. A file already recorded for this quarter is not hashed again, so
the stage can be re-run at any time and only picks up new files.
"""
import hashlib
from pathlib import Path

from .config import Config, _norm_folder, issuer_list, progress, setup_logging
from .db import connect
from .naming import slugify, submission_id


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(cfg: Config) -> dict:
    log = setup_logging(cfg, "scan")
    skip = cfg.raw.get("skip_prefix", "_")
    counts = {"folders": 0, "files": 0, "already_recorded": 0, "new_documents": 0,
              "duplicates": 0, "reused_from_earlier": 0, "failed": 0}

    folders = [f for f in sorted(cfg.input_root.iterdir(), key=lambda p: p.name.lower())
               if f.is_dir() and not f.name.startswith(skip)]
    wanted = issuer_list(cfg)
    if wanted is not None:
        all_n = len(folders)
        folders = [f for f in folders if _norm_folder(f.name) in wanted]
        found = {_norm_folder(f.name) for f in folders}
        missing = [v[0] for k, v in wanted.items() if k not in found]
        log.info("issuer list %s: %d of %d folders selected (%d entities); %d folders skipped",
                 cfg.raw.get("issuers_file"), len(folders), all_n, len({v[1] for v in wanted.values()}),
                 all_n - len(folders))
        for m in missing:
            log.warning("listed issuer has no folder in %s: %s", cfg.input_root, m)
    log.info("%d issuer folders to scan in %s", len(folders), cfg.input_root)
    progress.start(len(folders), "folders")
    with connect(cfg.database_url) as conn:
        for fi, folder in enumerate(folders, start=1):
            progress.step(fi, folder.name[:40])
            counts["folders"] += 1
            issuer_id = conn.execute(
                """INSERT INTO issuers (folder_name, bank_slug) VALUES (%s, %s)
                   ON CONFLICT (folder_name) DO UPDATE SET folder_name = EXCLUDED.folder_name
                   RETURNING issuer_id""",
                (folder.name, slugify(folder.name)),
            ).fetchone()["issuer_id"]

            for pdf in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
                if not pdf.is_file() or pdf.name.startswith(skip) or pdf.suffix.lower() != ".pdf":
                    continue
                counts["files"] += 1
                seen = conn.execute(
                    "SELECT 1 FROM document_files WHERE issuer_id=%s AND quarter=%s AND file_name=%s",
                    (issuer_id, cfg.quarter, pdf.name),
                ).fetchone()
                if seen:
                    counts["already_recorded"] += 1
                    continue
                try:
                    digest = sha256_of(pdf)
                except OSError as e:
                    counts["failed"] += 1
                    log.error("cannot read %s / %s: %s", folder.name, pdf.name, e)
                    continue

                # New content -> new documents row. Content seen in an earlier quarter (or the
                # pilot) is reused, so its text and extraction are not redone.
                new_doc = conn.execute(
                    "INSERT INTO documents (sha256) VALUES (%s) ON CONFLICT DO NOTHING RETURNING sha256",
                    (digest,),
                ).fetchone() is not None
                # A duplicate = the same content already recorded under another file THIS quarter.
                duplicate = conn.execute(
                    "SELECT 1 FROM document_files WHERE sha256=%s AND quarter=%s",
                    (digest, cfg.quarter),
                ).fetchone() is not None
                conn.execute(
                    """INSERT INTO document_files
                         (sha256, issuer_id, quarter, file_name, submission_id, size_bytes, is_duplicate)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (digest, issuer_id, cfg.quarter, pdf.name, submission_id(pdf.name),
                     pdf.stat().st_size, duplicate),
                )
                if new_doc:
                    counts["new_documents"] += 1
                elif duplicate:
                    counts["duplicates"] += 1
                    log.info("duplicate content: %s / %s", folder.name, pdf.name)
                else:
                    counts["reused_from_earlier"] += 1
            conn.commit()

    progress.stop()
    if wanted is not None:
        # documents of issuers that are not in the list (e.g. scanned before the list was set)
        others = conn_others(cfg, wanted)
        if others:
            log.warning("%d issuers already in the database are not in the issuer list (scanned earlier); "
                        "later stages would still process them. To drop them: next_steps.ps1 -ResetDb, "
                        "then run again. Examples: %s", len(others), "; ".join(others[:5]))
    log.info("scan done: %s", counts)
    return counts


def conn_others(cfg: Config, wanted: dict) -> list[str]:
    with connect(cfg.database_url) as conn:
        names = [r["folder_name"] for r in conn.execute(
            """SELECT DISTINCT i.folder_name FROM issuers i JOIN document_files f USING (issuer_id)
               WHERE f.quarter = %s ORDER BY 1""", (cfg.quarter,)).fetchall()]
    return [n for n in names if _norm_folder(n) not in wanted]
