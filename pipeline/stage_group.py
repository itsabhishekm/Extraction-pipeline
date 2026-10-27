"""Stage 4 - group: decide which PDFs make up each card version.

Rule (audit prompt): files of one issuer that share a submission ID form one version,
e.g. an agreement plus its pricing addendum. A file without a submission ID is its own
version. Two groups with exactly the same content (e.g. the same PDF submitted under two
IDs) are merged into one version. Failed documents are left out.

A group is only formed once all of its documents have text (stage 'text'), so a version
never waits on half its PDFs. Each version starts with a provisional card named after its
main file; the extract stage replaces it with the card name printed in the PDF.
"""
import re
from collections import defaultdict

from .config import Config, setup_logging
from .db import connect
from .naming import slugify

ROLE_PATTERNS = [
    ("application", r"application|solicitation"),
    ("rewards", r"reward.*(terms|program)|program.*terms|cash ?back program"),
    ("pricing", r"pricing|rate[s]? and fee|rate sheet|fee table|schumer|addendum|disclosure|schedule|important terms"),
    ("agreement", r"agreement|cardmember|cardholder|terms and conditions|\bcma\b|\bagree"),
]


def guess_role(file_name: str, page_count: int | None) -> str:
    name = file_name.lower()
    hits = [role for role, pat in ROLE_PATTERNS if re.search(pat, name)]
    if "pricing" in hits and "agreement" in hits:
        return "combined"
    if hits:
        return hits[0]
    return "agreement" if (page_count or 0) > 4 else "other"


def title_from_file(file_name: str) -> str:
    """'Citi_Double_Cash Agreement.pdf-256158.pdf' -> 'Citi Double Cash Agreement'."""
    t = re.sub(r"\.pdf-\d+\.pdf$", "", file_name, flags=re.IGNORECASE)
    t = re.sub(r"\.pdf$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[_]+", " ", t)
    return re.sub(r"\s+", " ", t).strip() or file_name


def run(cfg: Config) -> dict:
    log = setup_logging(cfg, "group")
    counts = {"groups_found": 0, "new_versions": 0, "already_grouped": 0, "waiting_for_text": 0,
              "merged_identical": 0}
    with connect(cfg.database_url) as conn:
        files = conn.execute(
            """SELECT f.issuer_id, i.folder_name, f.file_name, f.submission_id, f.file_id,
                      d.sha256, d.stage, d.status, d.page_count
               FROM document_files f
               JOIN documents d ON d.sha256 = f.sha256
               JOIN issuers i ON i.issuer_id = f.issuer_id
               WHERE f.quarter = %s
               ORDER BY f.issuer_id, f.file_id""", (cfg.quarter,)).fetchall()

        # issuer -> submission id (or file id) -> files
        groups = defaultdict(lambda: defaultdict(list))
        for f in files:
            if f["status"] != "ok":
                continue
            key = f["submission_id"] or f"file{f['file_id']}"
            groups[f["issuer_id"]][key].append(f)

        for issuer_id, by_id in groups.items():
            seen_content = {}
            for sub_id, members in sorted(by_id.items()):
                counts["groups_found"] += 1
                shas = sorted({m["sha256"] for m in members})
                if any(m["stage"] in ("scanned", "needs_ocr") for m in members):
                    counts["waiting_for_text"] += 1
                    continue
                content_key = "|".join(shas)
                if content_key in seen_content:
                    counts["merged_identical"] += 1
                    log.info("%s: submission %s has the same content as %s, not a separate version",
                             members[0]["folder_name"], sub_id, seen_content[content_key])
                    continue
                seen_content[content_key] = sub_id
                group_key = f"{issuer_id}:{sub_id}:{content_key}"
                if conn.execute("SELECT 1 FROM card_versions WHERE quarter=%s AND group_key=%s",
                                (cfg.quarter, group_key)).fetchone():
                    counts["already_grouped"] += 1
                    continue

                # one entry per content hash; the largest document names the provisional card
                docs = {}
                for m in members:
                    docs.setdefault(m["sha256"], m)
                main = max(docs.values(), key=lambda m: m["page_count"] or 0)
                title = title_from_file(main["file_name"])
                slug = slugify(title)[:60].rstrip("_") or f"card_{sub_id}"
                card_id = conn.execute(
                    """INSERT INTO cards (issuer_id, card_name, card_slug, card_name_source)
                       VALUES (%s, %s, %s, 'file_title')
                       ON CONFLICT (issuer_id, card_slug) DO UPDATE SET card_slug = EXCLUDED.card_slug
                       RETURNING card_id""", (issuer_id, title, slug)).fetchone()["card_id"]
                version_id = conn.execute(
                    """INSERT INTO card_versions (card_id, quarter, group_key, stage)
                       VALUES (%s, %s, %s, 'grouped') RETURNING version_id""",
                    (card_id, cfg.quarter, group_key)).fetchone()["version_id"]
                for sha, m in docs.items():
                    conn.execute(
                        "INSERT INTO version_documents (version_id, sha256, role) VALUES (%s, %s, %s)",
                        (version_id, sha, guess_role(m["file_name"], m["page_count"])))
                    conn.execute("UPDATE documents SET stage='grouped', updated_at=now() "
                                 "WHERE sha256=%s AND stage='text'", (sha,))
                counts["new_versions"] += 1
                log.info("%s: version %d = %s", main["folder_name"], version_id,
                         " + ".join(f"{m['file_name']} [{guess_role(m['file_name'], m['page_count'])}]"
                                    for m in docs.values()))
            conn.commit()
    log.info("group done: %s", counts)
    return counts
