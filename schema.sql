-- =====================================================================
-- schema.sql — charge-sheet (superbill) OCR pipeline persistence layer
--
-- Target DB: the pch/834 Postgres reached by auth.get_pg_connection
--            (pch-db-dev001 today; promote to myopsprod when it ships).
-- Schema:    wpo   (same schema family as the EOB / text-to-SQL catalog).
--
-- DOCUMENTATION ONLY — nothing here is executed by the pipeline. Run this
-- by hand (or fold it into your migration tooling) to create the tables the
-- code in db.py expects. It is idempotent (IF NOT EXISTS) and safe to re-run.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS wpo;

-- ---------------------------------------------------------------------
-- 1. Practice registry — GLOBAL and PRE-EXISTING. Practices live in the
--    Tebra EDI table  "EDI_Tebra".practice  (PK = id, name = prct_name).
--    We do NOT create or own it. At ingest the pipeline RESOLVES the global
--    practice (storage.PRACTICE, matched against prct_name) to its id and
--    stores that id + the resolved name on chargesheet_documents.
--
--    No FK points at it: the EDI table is loaded/reloaded by the Tebra feed,
--    and a hard cross-schema FK would break on a reload. practice_id is a
--    SOFT reference, and practice_name is denormalized onto the document so
--    reporting never depends on the EDI table being present.
--
--    Blob container is hardcoded in storage.py (CONTAINER); paths stem from
--    the practice name.
-- ---------------------------------------------------------------------
-- (no CREATE TABLE here — "EDI_Tebra".practice already exists)

-- ---------------------------------------------------------------------
-- 2. Template catalog store (moves catalog_*.json into the DB)
--    Follows the text-to-SQL admin catalog pattern: template_id + version,
--    background build + hot-reload, status gate. The whole catalog body
--    (fingerprint + sections + header_fields) lives in `body` as JSONB.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wpo.chargesheet_catalogs (
    template_id   TEXT        NOT NULL,
    version       INT         NOT NULL DEFAULT 1,
    status        TEXT        NOT NULL DEFAULT 'active',   -- active | draft | retired
    source        TEXT,
    fingerprint   JSONB       NOT NULL,   -- { header_labels[], anchor_codes[] }
    header_fields JSONB       NOT NULL,   -- ["date","name","dob",...]
    sections      JSONB       NOT NULL,   -- [ { section, code_type, cells[] } ]
    cell_count    INT,
    created_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (template_id, version)
);
CREATE INDEX IF NOT EXISTS ix_cs_catalogs_active
    ON wpo.chargesheet_catalogs (template_id) WHERE status = 'active';

-- ---------------------------------------------------------------------
-- 3. Documents — one row per source PDF processed (the "run" unit).
--    file_sha256 gives idempotent re-ingest (UNIQUE), matching the EOB
--    duplicate-ingestion guard. source_blob_path / pages_blob_prefix are
--    the "track it all (path)" requirement.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wpo.chargesheet_documents (
    document_id       BIGSERIAL PRIMARY KEY,
    practice_id       BIGINT,                       -- soft ref to "EDI_Tebra".practice.id (no FK: EDI table is reloaded)
    practice_name     TEXT        NOT NULL,          -- resolved prct_name, denormalized for reporting
    source_blob_path  TEXT        NOT NULL,        -- 834labs-sftp/{practice}/{file}.pdf
    pages_blob_prefix TEXT        NOT NULL,        -- 834labs-sftp/{practice}/pages/{stem}/
    file_name         TEXT        NOT NULL,
    file_sha256       TEXT        NOT NULL UNIQUE,  -- dedupe / resume
    page_count        INT,
    template_id       TEXT,
    status            TEXT        NOT NULL DEFAULT 'queued',  -- queued|processing|done|failed
    error             TEXT,
    run_metrics       JSONB,
    created_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_ts      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_cs_docs_practice ON wpo.chargesheet_documents (practice_id);
CREATE INDEX IF NOT EXISTS ix_cs_docs_status   ON wpo.chargesheet_documents (status);

-- ---------------------------------------------------------------------
-- 4. Pages — one row per page (one encounter). Stores the page image path,
--    the transcribed header, template-match provenance and page-level flags
--    (blank_header / duplicate_of_page_N). raw_result keeps the full model
--    JSON for audit.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wpo.chargesheet_pages (
    page_id         BIGSERIAL PRIMARY KEY,
    document_id     BIGINT      NOT NULL REFERENCES wpo.chargesheet_documents(document_id) ON DELETE CASCADE,
    page_number     INT         NOT NULL,
    page_blob_path  TEXT        NOT NULL,   -- .../pages/{stem}/page-NN.png
    template_ok     BOOLEAN,
    template_state  TEXT,                   -- known | autobuilt | miss
    template_score  NUMERIC(4,2),
    catalog_used    TEXT,
    header          JSONB,                  -- {date,name,dob,prn,insurance,copay,...}
    patient_name    TEXT,                   -- denormalized from header for dedup/search
    dob             TEXT,
    flags           JSONB,                  -- ["ambiguous_mark","duplicate_of_page_3",...]
    raw_result      JSONB,
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_number)
);
CREATE INDEX IF NOT EXISTS ix_cs_pages_doc  ON wpo.chargesheet_pages (document_id);
CREATE INDEX IF NOT EXISTS ix_cs_pages_name ON wpo.chargesheet_pages (patient_name, dob);

-- ---------------------------------------------------------------------
-- 5. Extractions — one row per code the pipeline surfaced. Covers confirmed
--    picks (circled_procedures / circled_diagnoses) AND possible_marks, kept
--    apart by `kind` + `status`. extraction_id is the stable handle that
--    "incorrect" feedback points at.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wpo.chargesheet_extractions (
    extraction_id BIGSERIAL PRIMARY KEY,
    page_id       BIGINT      NOT NULL REFERENCES wpo.chargesheet_pages(page_id) ON DELETE CASCADE,
    document_id   BIGINT      NOT NULL REFERENCES wpo.chargesheet_documents(document_id) ON DELETE CASCADE,
    page_number   INT         NOT NULL,
    kind          TEXT        NOT NULL,   -- procedure | diagnosis
    status        TEXT        NOT NULL,   -- confirmed | possible
    code          TEXT        NOT NULL,
    description   TEXT,
    section       TEXT,
    mark          TEXT,                   -- circle | check | underline | (null for possible)
    reason        TEXT,                   -- why uncertain (possible_marks only)
    confidence    NUMERIC(4,3),
    created_ts    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cs_extr_page ON wpo.chargesheet_extractions (page_id);
CREATE INDEX IF NOT EXISTS ix_cs_extr_doc  ON wpo.chargesheet_extractions (document_id);
CREATE INDEX IF NOT EXISTS ix_cs_extr_code ON wpo.chargesheet_extractions (code);

-- ---------------------------------------------------------------------
-- 6. Notes — handwritten clinical margin text, incl. codes written on the
--    sheet that are NOT on the printed form (handwritten_code_not_on_form).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wpo.chargesheet_notes (
    note_id       BIGSERIAL PRIMARY KEY,
    page_id       BIGINT      NOT NULL REFERENCES wpo.chargesheet_pages(page_id) ON DELETE CASCADE,
    document_id   BIGINT      NOT NULL REFERENCES wpo.chargesheet_documents(document_id) ON DELETE CASCADE,
    page_number   INT         NOT NULL,
    text          TEXT        NOT NULL,
    near          TEXT,
    off_form_code TEXT,       -- e.g. "M25.561" when the note references a code not on the form
    confidence    NUMERIC(4,3),
    created_ts    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cs_notes_page ON wpo.chargesheet_notes (page_id);

-- ---------------------------------------------------------------------
-- 7. Feedback — the human review loop. Exactly the two signals asked for:
--      feedback_type = 'incorrect'  -> what was identified is wrong (false positive)
--                                      references an existing extraction_id
--      feedback_type = 'missed'     -> what was NOT identified (false negative)
--                                      extraction_id is NULL; code is what the
--                                      reviewer says should have been captured
--    `target` widens this to header/note corrections without new tables.
--    These rows are what feed precision/recall per template and, later,
--    threshold tuning / prompt refinement / catalog fixes.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wpo.chargesheet_feedback (
    feedback_id   BIGSERIAL PRIMARY KEY,
    document_id   BIGINT      NOT NULL REFERENCES wpo.chargesheet_documents(document_id) ON DELETE CASCADE,
    page_number   INT         NOT NULL,
    extraction_id BIGINT      REFERENCES wpo.chargesheet_extractions(extraction_id) ON DELETE SET NULL,
    feedback_type TEXT        NOT NULL CHECK (feedback_type IN ('incorrect','missed')),
    target        TEXT        NOT NULL DEFAULT 'code'
                              CHECK (target IN ('code','procedure','diagnosis','note','header')),
    code          TEXT,       -- the code in question (both types)
    correct_code  TEXT,       -- corrected code, if the mark was misread
    correct_value TEXT,       -- corrected header field / note text
    section       TEXT,       -- for 'missed': which section the code sits in
    mark          TEXT,       -- for 'missed': circle | check | underline
    reviewer      TEXT,
    note          TEXT,
    applied       BOOLEAN     NOT NULL DEFAULT FALSE,  -- folded into tuning/catalog yet?
    created_ts    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cs_fb_doc  ON wpo.chargesheet_feedback (document_id, page_number);
CREATE INDEX IF NOT EXISTS ix_cs_fb_type ON wpo.chargesheet_feedback (feedback_type);

-- ---------------------------------------------------------------------
-- Convenience views for the review dashboard / quality metrics.
-- ---------------------------------------------------------------------

-- Per-document tally of the two feedback signals.
CREATE OR REPLACE VIEW wpo.vw_chargesheet_feedback_summary AS
SELECT d.document_id,
       d.practice_name,
       d.file_name,
       d.template_id,
       COUNT(*) FILTER (WHERE f.feedback_type = 'incorrect') AS false_positives,
       COUNT(*) FILTER (WHERE f.feedback_type = 'missed')    AS false_negatives,
       COUNT(*)                                              AS total_feedback,
       MAX(f.created_ts)                                     AS last_feedback_ts
FROM wpo.chargesheet_documents d
LEFT JOIN wpo.chargesheet_feedback f ON f.document_id = d.document_id
GROUP BY d.document_id, d.practice_name, d.file_name, d.template_id;

-- Precision/recall per template from confirmed extractions vs. feedback.
--   TP  = confirmed extractions NOT flagged 'incorrect'
--   FP  = 'incorrect' feedback rows
--   FN  = 'missed'    feedback rows
CREATE OR REPLACE VIEW wpo.vw_chargesheet_template_quality AS
WITH tp AS (
    SELECT e.document_id,
           COUNT(*) AS tp
    FROM wpo.chargesheet_extractions e
    WHERE e.status = 'confirmed'
      AND NOT EXISTS (SELECT 1 FROM wpo.chargesheet_feedback f
                      WHERE f.extraction_id = e.extraction_id
                        AND f.feedback_type = 'incorrect')
    GROUP BY e.document_id
),
fb AS (
    SELECT document_id,
           COUNT(*) FILTER (WHERE feedback_type = 'incorrect') AS fp,
           COUNT(*) FILTER (WHERE feedback_type = 'missed')    AS fn
    FROM wpo.chargesheet_feedback
    GROUP BY document_id
)
SELECT d.template_id,
       SUM(COALESCE(tp.tp,0)) AS true_positives,
       SUM(COALESCE(fb.fp,0)) AS false_positives,
       SUM(COALESCE(fb.fn,0)) AS false_negatives,
       ROUND( SUM(COALESCE(tp.tp,0))::numeric
              / NULLIF(SUM(COALESCE(tp.tp,0)) + SUM(COALESCE(fb.fp,0)),0), 3) AS precision,
       ROUND( SUM(COALESCE(tp.tp,0))::numeric
              / NULLIF(SUM(COALESCE(tp.tp,0)) + SUM(COALESCE(fb.fn,0)),0), 3) AS recall
FROM wpo.chargesheet_documents d
LEFT JOIN tp ON tp.document_id = d.document_id
LEFT JOIN fb ON fb.document_id = d.document_id
GROUP BY d.template_id;
