"""Read .md files back into pages, pick the pages a field group needs, and normalize
text so quotes can be matched against the page they came from."""
import html
import re
from dataclasses import dataclass
from pathlib import Path

PAGE_MARK = re.compile(r"^<!-- page (\d+)(?::\s*(ocr|needs_ocr))? -->\s*$", re.MULTILINE)


@dataclass(eq=False)  # compared by identity, so pages can be dict keys / set members
class Page:
    doc: str          # label used in prompts: D1, D2, ...
    sha256: str
    file_name: str
    role: str
    page_no: int
    text: str
    ocr: bool


def read_pages(md_path: str, doc: str, sha256: str, file_name: str, role: str) -> list[Page]:
    md = Path(md_path).read_text(encoding="utf-8")
    marks = list(PAGE_MARK.finditer(md))
    pages = []
    for i, m in enumerate(marks):
        if m.group(2) == "needs_ocr":
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(md)
        text = md[m.end():end].strip()
        pages.append(Page(doc, sha256, file_name, role, int(m.group(1)), text, m.group(2) == "ocr"))
    return pages


def select_pages(pages: list[Page], keywords: list[str], max_chars: int, first_pages: int = 0,
                 last_pages: int = 0, prefer_roles: tuple = (), anchors: tuple = ()) -> list[Page]:
    """Pages to send for one field group, returned in reading order:
    1. pages containing an anchor phrase (e.g. the pricing table heading) - always included
    2. the first / last pages of each document, if the group asks for them
    3. the rest by keyword hits per page length (so a short table page beats long prose that
       mentions "APR" many times), until max_chars.
    Matching uses normalized text (markdown, <br>, bold and table bars removed), because tables
    in the .md files look like "**Annual Percentage Rate**<br>**(APR) for Purchases**"."""
    kws = [normalize(k) for k in keywords]
    anc = [normalize(a) for a in anchors]
    norm = {id(p): normalize(p.text) for p in pages}

    def score(p: Page) -> float:
        low = norm[id(p)]
        hits = sum(low.count(k) for k in kws)
        s = hits * 1000 / (len(low) + 1000)
        if p.role in prefer_roles:
            s *= 1.5
        return s

    must = [p for p in pages if any(a in norm[id(p)] for a in anc)]
    by_doc = {}
    for p in pages:
        by_doc.setdefault(p.doc, []).append(p)
    for doc_pages in by_doc.values():
        must += doc_pages[:first_pages] + (doc_pages[-last_pages:] if last_pages else [])
    chosen, used = [], 0
    for p in dict.fromkeys(must):  # keep order, drop repeats
        chosen.append(p)
        used += len(p.text)
    for p in sorted((p for p in pages if p not in chosen), key=score, reverse=True):
        if score(p) <= 0 or used >= max_chars:
            break
        chosen.append(p)
        used += len(p.text)
    order = {id(p): i for i, p in enumerate(pages)}
    return sorted(chosen, key=lambda p: order[id(p)])


def render(pages: list[Page], max_chars: int) -> str:
    """Pages as prompt text, each under a header the model cites: [D1 page 3]."""
    out, used = [], 0
    for p in pages:
        room = max_chars - used
        if room <= 200:
            break
        body = p.text if len(p.text) <= room else p.text[:room] + "\n[...page cut...]"
        out.append(f"=== [{p.doc} page {p.page_no}] ===\n{body}")
        used += len(body)
    return "\n\n".join(out)


_MD = re.compile(r"(\*\*|__|`|<br\s*/?>|</?sup>|</?sub>|</?b>|</?i>|^#+\s*|\|)", re.IGNORECASE | re.MULTILINE)
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         " ": " ", "•": " "})


def normalize(text: str) -> str:
    """Lowercase, strip markdown/HTML markup, unify quotes and dashes, collapse whitespace."""
    t = html.unescape(text or "")
    t = _MD.sub(" ", t).translate(_QUOTES)
    t = re.sub(r"-{3,}", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()
