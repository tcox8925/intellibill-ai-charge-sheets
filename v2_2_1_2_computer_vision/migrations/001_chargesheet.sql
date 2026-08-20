-- Minimal charge-sheet extraction schema for PostgreSQL.
-- No worker/deployment/run-history tables are included.

CREATE SCHEMA IF NOT EXISTS wpo;

-- 1) Approved immutable form/template versions.
CREATE TABLE IF NOT EXISTS wpo.chargesheet_templates (
    template_id          TEXT        NOT NULL,
    version              INTEGER     NOT NULL,
    status               TEXT        NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active','retired')),
    form_name            TEXT        NOT NULL,
    reference_sha256     TEXT        NOT NULL,
    catalog_sha256       TEXT        NOT NULL,
    catalog              JSONB       NOT NULL,
    thresholds           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    policy               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_ts           TIMESTAMPTZ,
    PRIMARY KEY (template_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_chargesheet_templates_active
    ON wpo.chargesheet_templates(template_id)
    WHERE status = 'active';

-- 2) One row per source PDF.
CREATE TABLE IF NOT EXISTS wpo.chargesheet_documents (
    document_id        BIGSERIAL PRIMARY KEY,
    practice_id        BIGINT,
    practice_name      TEXT,
    source_path        TEXT,
    file_name          TEXT        NOT NULL,
    file_size_bytes    BIGINT,
    file_sha256        TEXT        NOT NULL,
    pipeline_version   TEXT        NOT NULL,
    created_ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (file_sha256)
);

-- 3) Current extraction for each PDF page.
CREATE TABLE IF NOT EXISTS wpo.chargesheet_pages (
    page_id                    BIGSERIAL PRIMARY KEY,
    document_id                BIGINT      NOT NULL
                                REFERENCES wpo.chargesheet_documents(document_id)
                                ON DELETE CASCADE,
    page_number                INTEGER     NOT NULL,
    page_sha256                TEXT        NOT NULL,
    pipeline_version           TEXT        NOT NULL,
    template_id                TEXT        NOT NULL,
    template_version           INTEGER     NOT NULL,
    template_ok                BOOLEAN     NOT NULL DEFAULT FALSE,
    template_match             NUMERIC(6,5),
    orb_inlier_ratio           NUMERIC(6,5),
    rotation_deg               INTEGER,
    residual_geometry_enabled  BOOLEAN,
    header                     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    header_notes_source        TEXT        NOT NULL DEFAULT 'none'
                                CHECK (header_notes_source IN ('model','cache','disabled','none','failed')),
    header_model               TEXT,
    header_extractor_version   TEXT,
    patient_name               TEXT,
    service_date               DATE,
    dob                        DATE,
    flags                      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    circle_detection           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    raw_result                 JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_number),
    FOREIGN KEY (template_id, template_version)
        REFERENCES wpo.chargesheet_templates(template_id, version)
);
CREATE INDEX IF NOT EXISTS ix_chargesheet_pages_patient
    ON wpo.chargesheet_pages(patient_name, dob);
CREATE INDEX IF NOT EXISTS ix_chargesheet_pages_header_cache
    ON wpo.chargesheet_pages(page_sha256, header_extractor_version, header_model);

-- 4) CONFIRMED CIRCLES ONLY. Checks, underlines and possible marks cannot be stored here.
CREATE TABLE IF NOT EXISTS wpo.chargesheet_selections (
    selection_id       BIGSERIAL PRIMARY KEY,
    page_id            BIGINT      NOT NULL
                        REFERENCES wpo.chargesheet_pages(page_id)
                        ON DELETE CASCADE,
    kind               TEXT        NOT NULL CHECK (kind IN ('procedure','diagnosis')),
    code               TEXT        NOT NULL,
    description        TEXT,
    section            TEXT,
    mark               TEXT        NOT NULL DEFAULT 'circle' CHECK (mark = 'circle'),
    detection_source   TEXT        NOT NULL
                        CHECK (detection_source IN ('color_geometry','residual_geometry')),
    confidence         NUMERIC(5,4),
    evidence           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, kind, code)
);
CREATE INDEX IF NOT EXISTS ix_chargesheet_selections_code
    ON wpo.chargesheet_selections(code);

-- 5) Header/notes model output. Billing-code selections never come from this table.
CREATE TABLE IF NOT EXISTS wpo.chargesheet_notes (
    note_id       BIGSERIAL PRIMARY KEY,
    page_id       BIGINT      NOT NULL
                  REFERENCES wpo.chargesheet_pages(page_id)
                  ON DELETE CASCADE,
    note_index    INTEGER     NOT NULL,
    text          TEXT        NOT NULL,
    near          TEXT,
    confidence    NUMERIC(5,4),
    created_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, note_index)
);

-- Convenience page-level output.
CREATE OR REPLACE VIEW wpo.vw_chargesheet_page_output AS
SELECT
    p.page_id,
    p.document_id,
    p.page_number,
    p.patient_name,
    p.service_date,
    p.dob,
    p.header,
    p.template_ok,
    p.template_match,
    p.flags,
    COALESCE(
        array_agg(s.code ORDER BY s.selection_id)
        FILTER (WHERE s.kind = 'procedure'),
        ARRAY[]::text[]
    ) AS procedure_codes,
    COALESCE(
        array_agg(s.code ORDER BY s.selection_id)
        FILTER (WHERE s.kind = 'diagnosis'),
        ARRAY[]::text[]
    ) AS diagnosis_codes
FROM wpo.chargesheet_pages p
LEFT JOIN wpo.chargesheet_selections s ON s.page_id = p.page_id
GROUP BY p.page_id;
