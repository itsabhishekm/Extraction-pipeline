"""Stage 8 - embed: BGE-M3 (dense + sparse) vectors of the agreement text -> Qdrant.

    python run.py embed            # versions at stage 'validated' -> 'embedded'
    python run.py search "can I opt out of arbitration" [--issuer "PNC BANK"] [--top 5]

What is embedded: the page text (.md) of every PDF in a validated card version, cut into
overlapping chunks that never cross a page, so every hit points to a file and page.
One Qdrant point per chunk of a PDF (a PDF shared by several cards - multi-card documents -
is stored once and lists all its cards / versions in the payload). Point ids are fixed
(uuid5 of sha256 + page + chunk), so re-running replaces points instead of adding copies.

Search is hybrid: the dense vector and the sparse (lexical) vector are queried separately and
merged with Reciprocal Rank Fusion in Qdrant.

Set CC_FAKE_EMBED=1 to use a hashing stand-in instead of BGE-M3 (testing without the model).
"""
import hashlib
import math
import os
import re
import uuid

from .config import Config, progress, setup_logging
from .db import connect, finish_run, start_run
from .pages import read_pages

DIM = 1024
NS = uuid.UUID("6f1c2a52-3c1e-4d7e-9a57-0c3b1f6f4a10")
HEADING = re.compile(r"^\s*(?:#{1,6}\s+(.+)|\*\*([^*]{4,120})\*\*\s*$|([A-Z][A-Z0-9 ,&'/()-]{6,80})\s*$)")


# ----------------------------------------------------------------------------- chunking
def chunk_page(text: str, size: int, overlap: int):
    """Return [(chunk_no, section, text)]: paragraphs packed into chunks of at most ~size chars;
    each new chunk starts with the last `overlap` chars of the previous one. Never crosses a page."""
    pieces, section = [], ""
    for para in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not para:
            continue
        m = HEADING.match(para.splitlines()[0])
        if m:
            section = next(g for g in m.groups() if g).strip()[:120]
        while len(para) > size:                      # a very long paragraph or table
            pieces.append((section, para[:size]))
            para = para[size - overlap:]
        pieces.append((section, para))
    chunks, buf, sec = [], "", ""
    for section, para in pieces:
        if buf and len(buf) + len(para) + 2 > size:
            chunks.append((sec, buf))
            tail = buf[-overlap:] if overlap else ""
            buf, sec = (tail + "\n\n" + para) if tail else para, section
            continue
        if not buf:
            sec = section
        buf = (buf + "\n\n" + para) if buf else para
    if buf:
        chunks.append((sec, buf))
    return [(i, s, t) for i, (s, t) in enumerate(chunks)]


def doc_chunks(md_path, sha256, file_name, size, overlap):
    for p in read_pages(md_path, "D1", sha256, file_name, ""):
        for no, section, text in chunk_page(p.text, size, overlap):
            yield {"id": str(uuid.uuid5(NS, f"{sha256}:{p.page_no}:{no}")), "sha256": sha256, "file_name": file_name,
                   "page": p.page_no, "chunk": no, "section": section, "text": text, "ocr": p.ocr}


# ----------------------------------------------------------------------------- embedder
class FakeEmbedder:
    """Deterministic hashing vectors with the same shapes as BGE-M3 (tests only)."""

    def encode(self, texts):
        dense, sparse = [], []
        for t in texts:
            toks = re.findall(r"[a-z0-9]+", t.lower())
            v = [0.0] * DIM
            w = {}
            for tok in toks:
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % DIM] += 1.0
                w[h % 250000] = w.get(h % 250000, 0.0) + 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            dense.append([x / n for x in v])
            sparse.append({k: 1.0 + math.log(c) for k, c in w.items()})
        return dense, sparse


class BGEM3:
    def __init__(self, path, batch_size=8, max_length=1024):
        from FlagEmbedding import BGEM3FlagModel
        import torch
        self.model = BGEM3FlagModel(str(path), use_fp16=torch.cuda.is_available())
        self.batch_size, self.max_length = batch_size, max_length

    def encode(self, texts):
        out = self.model.encode(list(texts), batch_size=self.batch_size, max_length=self.max_length,
                                return_dense=True, return_sparse=True, return_colbert_vecs=False)
        dense = [list(map(float, v)) for v in out["dense_vecs"]]
        sparse = [{int(k): float(v) for k, v in w.items()} for w in out["lexical_weights"]]
        return dense, sparse


_embedder = None


def embedder(cfg):
    global _embedder
    if _embedder is None:
        e = cfg.section("embed")
        _embedder = FakeEmbedder() if os.environ.get("CC_FAKE_EMBED") == "1" else \
            BGEM3(cfg.section("models")["bge_m3_path"], e.get("batch_size", 8), e.get("max_length", 1024))
    return _embedder


# ----------------------------------------------------------------------------- qdrant
def qdrant(cfg):
    from qdrant_client import QdrantClient
    q = cfg.section("qdrant")
    loc = q.get("url", "http://localhost:6333")
    if loc.startswith("http"):
        return QdrantClient(url=loc, timeout=60)
    return QdrantClient(path=loc)


def collection_name(cfg):
    return cfg.section("qdrant").get("collection", "cc_agreements")


def ensure_collection(client, name):
    from qdrant_client import models as m
    if client.collection_exists(name):
        return
    client.create_collection(
        name, vectors_config={"dense": m.VectorParams(size=DIM, distance=m.Distance.COSINE)},
        sparse_vectors_config={"sparse": m.SparseVectorParams(modifier=None)})
    for field, kind in [("issuer", m.PayloadSchemaType.KEYWORD), ("quarter", m.PayloadSchemaType.KEYWORD),
                        ("sha256", m.PayloadSchemaType.KEYWORD), ("version_ids", m.PayloadSchemaType.INTEGER),
                        ("card_names", m.PayloadSchemaType.KEYWORD)]:
        try:
            client.create_payload_index(name, field, kind)
        except Exception:  # noqa: BLE001 - local mode has no payload indexes
            pass


def _sparse(w):
    from qdrant_client import models as m
    items = sorted(w.items())
    return m.SparseVector(indices=[k for k, _ in items], values=[v for _, v in items])


# ----------------------------------------------------------------------------- stage
def run(cfg: Config, redo: bool = False) -> dict:
    from qdrant_client import models as m
    log = setup_logging(cfg, "embed")
    e = cfg.section("embed")
    size, overlap = e.get("chunk_chars", 1200), e.get("overlap_chars", 200)
    counts = {"versions": 0, "documents": 0, "chunks": 0}
    client, name = qdrant(cfg), collection_name(cfg)
    ensure_collection(client, name)
    with connect(cfg.database_url) as conn:
        run_id = start_run(conn, cfg, "embed")
        stages = ("validated", "embedded") if redo else ("validated",)
        versions = conn.execute("SELECT version_id FROM card_versions WHERE quarter=%s AND stage = ANY(%s) "
                                "ORDER BY version_id", (cfg.quarter, list(stages))).fetchall()
        vids = [v["version_id"] for v in versions]
        docs = conn.execute(
            """SELECT DISTINCT vd.sha256, d.md_path, f.file_name, i.folder_name
               FROM version_documents vd JOIN documents d USING (sha256)
               JOIN LATERAL (SELECT file_name, issuer_id FROM document_files x WHERE x.sha256 = vd.sha256
                             ORDER BY file_id LIMIT 1) f ON true
               JOIN issuers i ON i.issuer_id = f.issuer_id
               WHERE vd.version_id = ANY(%s)""", (vids,)).fetchall()
        log.info("%d versions, %d PDFs to embed into %s", len(vids), len(docs), name)
        progress.start(len(docs), "PDFs")
        for di, d in enumerate(docs, start=1):
            progress.step(di, d["folder_name"][:30])
            try:
                # every version / card of this quarter that uses the PDF
                users = conn.execute(
                    """SELECT v.version_id, c.card_name, v.version_date FROM version_documents vd
                       JOIN card_versions v USING (version_id) JOIN cards c USING (card_id)
                       WHERE vd.sha256=%s AND v.quarter=%s ORDER BY v.version_id""",
                    (d["sha256"], cfg.quarter)).fetchall()
                chunks = list(doc_chunks(d["md_path"], d["sha256"], d["file_name"], size, overlap))
                if not chunks:
                    continue
                # drop points of this PDF from an earlier run (chunking may have changed)
                client.delete(name, points_selector=m.FilterSelector(filter=m.Filter(must=[
                    m.FieldCondition(key="sha256", match=m.MatchValue(value=d["sha256"]))])))
                for i in range(0, len(chunks), 32):
                    batch = chunks[i:i + 32]
                    dense, sparse = embedder(cfg).encode([c["text"] for c in batch])
                    client.upsert(name, points=[m.PointStruct(
                        id=c["id"], vector={"dense": dv, "sparse": _sparse(sv)},
                        payload={"issuer": d["folder_name"], "quarter": cfg.quarter, "sha256": c["sha256"],
                                 "file_name": c["file_name"], "page": c["page"], "chunk": c["chunk"],
                                 "section": c["section"], "text": c["text"], "ocr": c["ocr"],
                                 "version_ids": [u["version_id"] for u in users],
                                 "card_names": sorted({u["card_name"] for u in users}),
                                 "version_dates": sorted({str(u["version_date"]) for u in users if u["version_date"]}),
                                 "run_id": run_id})
                        for c, dv, sv in zip(batch, dense, sparse)])
                counts["documents"] += 1
                counts["chunks"] += len(chunks)
                log.info("%s / %s: %d chunks", d["folder_name"], d["file_name"], len(chunks))
            except Exception as ex:  # noqa: BLE001
                log.exception("embedding %s FAILED: %s", d["file_name"], ex)
                conn.execute("UPDATE documents SET last_error=%s WHERE sha256=%s", (f"embed: {ex}"[:500], d["sha256"]))
                conn.commit()
                vids = [v for v in vids if not conn.execute(
                    "SELECT 1 FROM version_documents WHERE version_id=%s AND sha256=%s", (v, d["sha256"])).fetchone()]
        conn.execute("UPDATE card_versions SET stage='embedded' WHERE version_id = ANY(%s)", (vids,))
        conn.commit()
        counts["versions"] = len(vids)
        finish_run(conn, run_id)
    progress.stop()
    log.info("embed done: %s", counts)
    return counts


def expected_chunks(cfg, conn) -> int:
    """How many points Qdrant should hold for this quarter (used by the integrity check)."""
    e = cfg.section("embed")
    docs = conn.execute(
        """SELECT DISTINCT vd.sha256, d.md_path FROM version_documents vd JOIN documents d USING (sha256)
           JOIN card_versions v USING (version_id) WHERE v.quarter=%s AND v.stage='embedded'""",
        (cfg.quarter,)).fetchall()
    return sum(len(list(doc_chunks(d["md_path"], d["sha256"], "", e.get("chunk_chars", 1200),
                                   e.get("overlap_chars", 200)))) for d in docs)


def search(cfg: Config, query: str, top: int = 5, issuer: str | None = None, card: str | None = None,
           client=None):
    from qdrant_client import models as m
    client = client or qdrant(cfg)
    dense, sparse = embedder(cfg).encode([query])
    must = [m.FieldCondition(key="quarter", match=m.MatchValue(value=cfg.quarter))]
    if issuer:
        must.append(m.FieldCondition(key="issuer", match=m.MatchValue(value=issuer)))
    if card:
        must.append(m.FieldCondition(key="card_names", match=m.MatchAny(any=[card])))
    flt = m.Filter(must=must)
    res = client.query_points(
        collection_name(cfg),
        prefetch=[m.Prefetch(query=dense[0], using="dense", limit=top * 5, filter=flt),
                  m.Prefetch(query=_sparse(sparse[0]), using="sparse", limit=top * 5, filter=flt)],
        query=m.FusionQuery(fusion=m.Fusion.RRF), limit=top, with_payload=True)
    return [{"score": round(p.score, 4), **{k: p.payload.get(k) for k in
                                            ("issuer", "card_names", "file_name", "page", "section", "text")}}
            for p in res.points]


def print_search(cfg, query, top=5, issuer=None, card=None):
    hits = search(cfg, query, top, issuer, card)
    for i, h in enumerate(hits, 1):
        print(f"\n{i}. [{h['score']}] {h['issuer']} | {', '.join(h['card_names'] or [])}")
        print(f"   {h['file_name']} page {h['page']}" + (f" - {h['section']}" if h["section"] else ""))
        print("   " + re.sub(r"\s+", " ", h["text"])[:300])
    if not hits:
        print("no results")
    return hits
