"""Naming rules from the audit prompt, used for bank and card slugs."""
import re

_SUBMISSION_ID = re.compile(r"\.pdf-(\d+)\.pdf$", re.IGNORECASE)


def slugify(name: str) -> str:
    """lowercase; % -> pct; drop (R) and (TM); runs of spaces/punctuation -> single _."""
    s = name.lower().replace("%", "pct").replace("®", "").replace("™", "")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def submission_id(file_name: str) -> str | None:
    """'<original name>.pdf-<submission_id>.pdf' -> submission_id."""
    m = _SUBMISSION_ID.search(file_name)
    return m.group(1) if m else None
