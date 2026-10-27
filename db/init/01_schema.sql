-- =====================================================================
-- CFPB credit card agreements: PostgreSQL data model (v1)
-- Mirrors audit_prompt_v6: issuers -> cards -> versions -> field values,
-- with every value traceable to a PDF (sha256), a page, a quote, a model
-- run and a prompt version.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS cc;
SET search_path = cc, public;

-- ---------- fixed value lists (from the audit prompt) ----------------
CREATE TYPE field_status   AS ENUM ('found','stated_none','not_applicable','not_present','unreadable','ambiguous');
CREATE TYPE confidence_lvl AS ENUM ('high','medium','low');
CREATE TYPE location_kind  AS ENUM ('text','table','image','footnote');
CREATE TYPE approach_tag   AS ENUM ('TEXT_EXTRACT','TABLE_EXTRACT','OCR','AI_ASSISTED','HYBRID','MANUAL_REVIEW');
CREATE TYPE text_source    AS ENUM ('pymupdf','glm-ocr','mixed');
CREATE TYPE doc_type       AS ENUM ('pricing_disclosure','cardholder_agreement','combined','application_disclosure',
                                    'multi_card_disclosure','rewards_terms','privacy_notice','other');
CREATE TYPE validation_state AS ENUM ('pending','passed','failed','retry_queued','human_review','human_approved');

-- ---------- 1. issuers: one row per bank folder ----------------------
CREATE TABLE issuers (
    issuer_id          SERIAL PRIMARY KEY,
    folder_name        TEXT NOT NULL UNIQUE,          -- exact CFPB folder name
    bank_slug          TEXT NOT NULL UNIQUE,          -- naming rule from the prompt (pnc_bank_n_a)
    issuer_name_in_pdf TEXT,                          -- legal name as printed
    charter_hint       TEXT,                          -- "N.A.", "Federal Credit Union", ...
    institution_type   TEXT,                          -- filled LATER from FDIC/NCUA, never from PDFs [15]
    audit_owner        TEXT CHECK (audit_owner IN ('Abhi','Dami','Kira')),
    contact_phone      TEXT[],
    contact_website    TEXT,
    contact_address    TEXT
);

-- ---------- 2. documents: one row per UNIQUE PDF content -------------
-- Keyed by sha256 so duplicates are stored and processed once, and
-- unchanged files are reused across quarters.
CREATE TABLE documents (
    sha256          CHAR(64) PRIMARY KEY,
    document_type   doc_type,
    languages       TEXT[],
    page_count      INT,
    file_size_kb    NUMERIC(10,1),
    pdf_version     TEXT,
    producer        TEXT,
    pdf_created     TEXT,                              -- exactly as the metadata tool returns it
    pdf_modified    TEXT,
    encrypted       BOOLEAN,
    content_type    TEXT,                              -- pure_text | tables | scanned_images | mixed_*
    text_source     text_source,                       -- how the .md was produced
    md_path         TEXT,                              -- <data_root>\text\<issuer>\<file>.md
    extraction_tag  approach_tag,
    extraction_tag_reason TEXT,
    layout_notes    TEXT[],
    errors          TEXT[],
    -- manifest: where this PDF is in the pipeline
    stage           TEXT NOT NULL DEFAULT 'scanned'
                    CHECK (stage IN ('scanned','needs_ocr','text','grouped','extracted','validated','loaded')),
    status          TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','failed')),
    last_error      TEXT,
    text_pages      INT,                               -- pages read from the embedded text
    ocr_pages       INT[] DEFAULT '{}',                -- page numbers that need / got OCR
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON documents (stage, status);

-- ---------- 3. document_files: every file occurrence -----------------
-- The same content can appear under several names / submission IDs /
-- quarters. One row per file actually submitted.
CREATE TABLE document_files (
    file_id         SERIAL PRIMARY KEY,
    sha256          CHAR(64) NOT NULL REFERENCES documents(sha256),
    issuer_id       INT NOT NULL REFERENCES issuers(issuer_id),
    quarter         TEXT NOT NULL,                     -- '2026Q1'
    file_name       TEXT NOT NULL,                     -- exact on-disk name (may contain garbled chars)
    submission_id   TEXT,                              -- from the file name; never used to order versions
    size_bytes      BIGINT,
    is_duplicate    BOOLEAN NOT NULL DEFAULT FALSE,    -- same content already seen under another file
    found_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (issuer_id, quarter, file_name)
);
CREATE INDEX ON document_files (sha256);

-- ---------- 4. page_text: optional copy of the .md text per page -----
-- The .md files are the source; load them here for quote checking in SQL.
CREATE TABLE page_text (
    sha256      CHAR(64) REFERENCES documents(sha256),
    page_no     INT,
    source      text_source NOT NULL,
    model_version TEXT,                                -- e.g. glm-ocr-0.9b; NULL for pymupdf
    text_md     TEXT NOT NULL,
    PRIMARY KEY (sha256, page_no)
);

-- ---------- 5. cards: one row per distinct product -------------------
CREATE TABLE cards (
    card_id          SERIAL PRIMARY KEY,
    issuer_id        INT NOT NULL REFERENCES issuers(issuer_id),
    card_name        TEXT NOT NULL,                    -- as written in the PDF
    card_slug        TEXT NOT NULL,                    -- bank__card file-name rule
    card_name_source TEXT NOT NULL DEFAULT 'pdf' CHECK (card_name_source IN ('pdf','file_title')),
    UNIQUE (issuer_id, card_slug)
);

-- ---------- 6. card_versions: one dated edition of a card's terms ----
CREATE TABLE card_versions (
    version_id          SERIAL PRIMARY KEY,
    card_id             INT NOT NULL REFERENCES cards(card_id),
    quarter             TEXT NOT NULL,                 -- quarter the version was submitted in
    version_date        DATE,
    version_date_source TEXT CHECK (version_date_source IN ('effective_date','file_name','pdf_created')),
    effective_date      DATE,                          -- only the date printed in the PDF
    is_latest           BOOLEAN NOT NULL DEFAULT FALSE,
    audit_level         TEXT NOT NULL DEFAULT 'full' CHECK (audit_level IN ('full','inventory_only')),
    card_type           TEXT CHECK (card_type IN ('consumer','business')),
    market_scope        TEXT CHECK (market_scope IN ('us_consumer','non_us','unclear')),
    secured             BOOLEAN,
    comparison_eligible BOOLEAN,
    eligibility_reasons TEXT[] DEFAULT '{}',
    terms_as_of         TEXT,                          -- "Terms as of March 27, 2023 (date printed in the agreement)"
    needs_manual_review BOOLEAN NOT NULL DEFAULT FALSE,
    review_reasons      TEXT[] DEFAULT '{}',
    -- pipeline bookkeeping
    group_key           TEXT,                          -- issuer + submission id + content hashes that formed it
    stage               TEXT NOT NULL DEFAULT 'grouped'
                        CHECK (stage IN ('grouped','extracted','validated','embedded')),
    json_path           TEXT,                          -- the card JSON file this version is written to
    -- multi-card documents: one version per product, all sharing the same PDFs
    product_name        TEXT,                          -- the product this version is about (NULL = single-card)
    products_in_group   TEXT[] DEFAULT '{}'            -- every product the PDFs cover
);
CREATE INDEX ON card_versions (card_id, version_date DESC);
CREATE UNIQUE INDEX ON card_versions (quarter, group_key);
-- at most one latest version per card
CREATE UNIQUE INDEX one_latest_per_card ON card_versions (card_id) WHERE is_latest;

-- ---------- 7. version_documents: which PDFs make up a version --------
CREATE TABLE version_documents (
    version_id  INT REFERENCES card_versions(version_id) ON DELETE CASCADE,
    sha256      CHAR(64) REFERENCES documents(sha256),
    role        TEXT CHECK (role IN ('agreement','pricing','combined','application','rewards','other')),
    PRIMARY KEY (version_id, sha256)
);

-- ---------- 8. field_definitions: the 31 target fields ---------------
CREATE TABLE field_definitions (
    field_name        TEXT PRIMARY KEY,
    field_group       TEXT NOT NULL,                   -- Card identification | Goal 1: Pricing | Goal 2: Legal rights
    project_target    INT,                             -- [1]..[15]
    normalized_format TEXT,
    needs_reasoning   BOOLEAN NOT NULL DEFAULT FALSE   -- run with Qwen thinking on
);

-- ---------- 9. pipeline_runs: model + prompt versions (lineage) ------
CREATE TABLE pipeline_runs (
    run_id          SERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    quarter         TEXT NOT NULL,
    ocr_model       TEXT,                              -- glm-ocr-0.9b-q8
    extraction_model TEXT NOT NULL,                    -- qwen3.5-4b-q4_k_m
    prompt_version  TEXT NOT NULL,                     -- extract-v1
    code_version    TEXT,                              -- git commit
    notes           TEXT
);

-- ---------- 10. field_values: one row per field per card version -----
CREATE TABLE field_values (
    value_id          BIGSERIAL PRIMARY KEY,
    version_id        INT  NOT NULL REFERENCES card_versions(version_id) ON DELETE CASCADE,
    field_name        TEXT NOT NULL REFERENCES field_definitions(field_name),
    status            field_status NOT NULL,
    raw_value         TEXT,                            -- exactly as written
    normalized_value  JSONB,                           -- shape per NORMALIZED VALUE FORMATS
    quote             TEXT,                            -- verbatim source text
    source_sha256     CHAR(64) REFERENCES documents(sha256),
    page_no           INT,
    location_type     location_kind,
    display           TEXT NOT NULL,                   -- app label fixed by status
    confidence        confidence_lvl,
    approach          approach_tag,
    note              TEXT,
    -- validation / repair
    validation        validation_state NOT NULL DEFAULT 'pending',
    validation_errors TEXT[] DEFAULT '{}',
    attempts          INT NOT NULL DEFAULT 1,
    run_id            INT REFERENCES pipeline_runs(run_id),
    UNIQUE (version_id, field_name),
    -- a found / stated_none value must carry its evidence
    CHECK (status NOT IN ('found','stated_none') OR (quote IS NOT NULL AND source_sha256 IS NOT NULL AND page_no IS NOT NULL))
);
CREATE INDEX ON field_values (field_name, status);
CREATE INDEX ON field_values (validation);

-- ---------- 11. conflicts: same version, two different values ---------
CREATE TABLE conflicts (
    conflict_id  SERIAL PRIMARY KEY,
    version_id   INT REFERENCES card_versions(version_id) ON DELETE CASCADE,
    field_name   TEXT REFERENCES field_definitions(field_name),
    value_a      TEXT, sha256_a CHAR(64), page_a INT,
    value_b      TEXT, sha256_b CHAR(64), page_b INT,
    resolved     BOOLEAN NOT NULL DEFAULT FALSE,
    resolution   TEXT
);

-- ---------- 12. review_items: human review queue ----------------------
-- (the automatic retry queue stays in retry_queue.jsonl; only what the
--  Repair Agent cannot fix lands here)
CREATE TABLE review_items (
    review_id    SERIAL PRIMARY KEY,
    value_id     BIGINT REFERENCES field_values(value_id) ON DELETE CASCADE,
    reason       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewer     TEXT,
    decision     TEXT CHECK (decision IN ('approved','corrected','rejected')),
    corrected_value JSONB,
    decided_at   TIMESTAMPTZ
);

-- ---------- 13. regulations: reference list of laws/regulations -------
CREATE TABLE regulations (
    reg_code     TEXT PRIMARY KEY,                     -- 'TILA_REGZ', 'MLA', ...
    name         TEXT NOT NULL,
    citation     TEXT,                                 -- '12 CFR 1026'
    category     TEXT CHECK (category IN ('disclosure','consumer_right','military','dispute','credit_reporting','privacy','consent','state')),
    typical_text TEXT,                                 -- what it usually looks like in an agreement
    search_terms TEXT[]                                -- keywords used to find candidates in the .md text
);

-- ---------- 14. regulation_mentions: regulations found in a card version
CREATE TABLE regulation_mentions (
    mention_id    BIGSERIAL PRIMARY KEY,
    version_id    INT  NOT NULL REFERENCES card_versions(version_id) ON DELETE CASCADE,
    reg_code      TEXT NOT NULL REFERENCES regulations(reg_code),
    mention_type  TEXT CHECK (mention_type IN ('cited_by_name','notice_included','right_granted','exception','indirect_reference')),
    state         CHAR(2),                             -- for state-law notices (e.g. 'WI', 'OH')
    quote         TEXT NOT NULL,                       -- verbatim text that mentions it
    source_sha256 CHAR(64) NOT NULL REFERENCES documents(sha256),
    page_no       INT NOT NULL,
    confidence    confidence_lvl,
    validation    validation_state NOT NULL DEFAULT 'pending',
    run_id        INT REFERENCES pipeline_runs(run_id),
    UNIQUE (version_id, reg_code, mention_type, state)
);
CREATE INDEX ON regulation_mentions (reg_code);

-- =====================================================================
-- Views for the app / analysis
-- =====================================================================

-- Latest terms per card, key pricing fields flattened (only validated values)
CREATE VIEW v_card_latest AS
SELECT i.folder_name AS issuer, i.charter_hint, c.card_name, v.version_date, v.terms_as_of,
       v.card_type, v.market_scope, v.secured, v.comparison_eligible,
       MAX(CASE WHEN f.field_name='purchase_apr' THEN (f.normalized_value->>'min')::numeric END) AS purchase_apr_min,
       MAX(CASE WHEN f.field_name='purchase_apr' THEN (f.normalized_value->>'max')::numeric END) AS purchase_apr_max,
       MAX(CASE WHEN f.field_name='annual_fee'   THEN (f.normalized_value->>'max_usd')::numeric END) AS annual_fee_max,
       MAX(CASE WHEN f.field_name='late_fee'     THEN (f.normalized_value->>'max_usd')::numeric END) AS late_fee_max,
       BOOL_OR(CASE WHEN f.field_name='arbitration_clause' THEN (f.normalized_value)::text::boolean END) AS arbitration,
       MAX(CASE WHEN f.field_name='governing_law_state' THEN f.normalized_value->>0 END)  AS governing_law
FROM card_versions v
JOIN cards c   ON c.card_id = v.card_id
JOIN issuers i ON i.issuer_id = c.issuer_id
LEFT JOIN field_values f ON f.version_id = v.version_id AND f.validation IN ('passed','human_approved')
WHERE v.is_latest
GROUP BY i.folder_name, i.charter_hint, c.card_name, v.version_date, v.terms_as_of,
         v.card_type, v.market_scope, v.secured, v.comparison_eligible;

-- Field coverage (the "cards_with_field" count from the prompt)
CREATE VIEW v_field_coverage AS
SELECT f.field_name,
       COUNT(*) FILTER (WHERE f.status IN ('found','stated_none')) AS cards_with_field,
       COUNT(*) AS cards_total
FROM field_values f JOIN card_versions v ON v.version_id = f.version_id AND v.is_latest
GROUP BY f.field_name;

-- Prior vs current terms [16-17]: compare each card's latest version with the one before
CREATE VIEW v_field_changes AS
WITH ranked AS (
  SELECT v.*, ROW_NUMBER() OVER (PARTITION BY v.card_id ORDER BY v.version_date DESC NULLS LAST) AS rn
  FROM card_versions v WHERE v.audit_level = 'full'
)
SELECT c.card_name, cur.version_date AS current_version_date, prev.version_date AS prior_version_date,
       fc.field_name, fp.normalized_value AS prior_value, fc.normalized_value AS current_value
FROM ranked cur
JOIN ranked prev ON prev.card_id = cur.card_id AND prev.rn = 2
JOIN cards c ON c.card_id = cur.card_id
JOIN field_values fc ON fc.version_id = cur.version_id
JOIN field_values fp ON fp.version_id = prev.version_id AND fp.field_name = fc.field_name
WHERE cur.rn = 1 AND fc.normalized_value IS DISTINCT FROM fp.normalized_value;

-- Which regulations each card's latest version mentions
CREATE VIEW v_card_regulations AS
SELECT i.folder_name AS issuer, c.card_name, r.reg_code, r.name AS regulation,
       m.mention_type, m.state, m.page_no, m.quote
FROM regulation_mentions m
JOIN card_versions v ON v.version_id = m.version_id AND v.is_latest
JOIN cards c   ON c.card_id = v.card_id
JOIN issuers i ON i.issuer_id = c.issuer_id
JOIN regulations r ON r.reg_code = m.reg_code
WHERE m.validation IN ('passed','human_approved');
