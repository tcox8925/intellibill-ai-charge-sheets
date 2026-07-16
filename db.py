"""
db.py — Postgres persistence + feedback for the charge-sheet pipeline.

Writes the pipeline output into the wpo.chargesheet_* tables (see schema.sql)
and records the two review signals:
    record_feedback(feedback_type='incorrect', extraction_id=...)  -> false positive
    record_feedback(feedback_type='missed',    code=...)           -> false negative

Auth reuses EOB's auth.get_pg_connection (KV -> AAD token -> psycopg2), so this
inherits the same KV/VNet story as pch-eob-pipeline. Nothing is hardcoded here.
"""

import json
import hashlib
from typing import Optional

from auth import get_kv_client, get_pg_connection, reconnect_if_stale

SCHEMA = "wpo"
ATTACHMENTS_TABLE = '"EDI_Tebra".attachments'


def _conn():
    kv = get_kv_client()
    return reconnect_if_stale(get_pg_connection(kv), kv)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_attachment_processed(clm_att_path: str, conn=None) -> bool:
    """Return the processed flag for an attachment row keyed by clm_att_path."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT COALESCE(processed, false)
                      FROM {ATTACHMENTS_TABLE}
                     WHERE clm_att_path=%s
                     LIMIT 1""",
                (clm_att_path,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False
    finally:
        if own:
            conn.close()


def mark_attachment_processed(clm_att_path: str, processed: bool = True,
                              conn=None) -> bool:
    """Update the processed flag for an attachment row keyed by clm_att_path."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {ATTACHMENTS_TABLE}
                       SET processed=%s,
                           updated_at=now()
                     WHERE clm_att_path=%s""",
                (processed, clm_att_path),
            )
            updated = cur.rowcount > 0
        if own:
            conn.commit()
        return updated
    finally:
        if own:
            conn.close()


def list_attachment_entries(limit: int = 10, conn=None):
    """Return the first attachment rows as dictionaries."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT *
                      FROM {ATTACHMENTS_TABLE}
                     ORDER BY 1
                     LIMIT %s""",
                (limit,),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        if own:
            conn.close()


# ---------- practice (global, from the Tebra EDI table) --------------------
# The practice table is pre-existing and owned by the Tebra EDI feed. We only
# READ it, matching storage.PRACTICE against prct_name.
# >>> UPDATE HERE if the practice table / column names ever change. <<<
PRACTICE_TABLE    = '"EDI_Tebra".practice'
PRACTICE_ID_COL   = "id"
PRACTICE_NAME_COL = "prct_name"


def resolve_practice(practice_name: str, conn=None):
    """Look up the global practice in "EDI_Tebra".practice by name (the folder
    name in storage.PRACTICE). Returns (practice_id | None, canonical_name).
    Case/whitespace-insensitive match; read-only (never writes the EDI table)."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT {PRACTICE_ID_COL}, {PRACTICE_NAME_COL}
                      FROM {PRACTICE_TABLE}
                     WHERE lower(btrim({PRACTICE_NAME_COL})) = lower(btrim(%s))
                     LIMIT 1""",
                (practice_name,),
            )
            row = cur.fetchone()
        return (row[0], row[1]) if row else (None, practice_name)
    finally:
        if own:
            conn.close()


# ---------- documents -------------------------------------------------------

def create_document(practice_id, practice_name: str, source_blob_path: str,
                    pages_blob_prefix: str, file_name: str, file_sha256: str,
                    conn=None) -> int:
    """Register a source PDF as a run. Idempotent on file_sha256: a re-ingest
    of the same bytes returns the existing document_id (resume-friendly).
    practice_id may be None if the practice wasn't found in "EDI_Tebra".practice;
    practice_name is always stored."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SCHEMA}.chargesheet_documents
                        (practice_id, practice_name, source_blob_path,
                         pages_blob_prefix, file_name, file_sha256, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'queued')
                    ON CONFLICT (file_sha256) DO UPDATE
                        SET pages_blob_prefix = EXCLUDED.pages_blob_prefix
                    RETURNING document_id""",
                (practice_id, practice_name, source_blob_path, pages_blob_prefix,
                 file_name, file_sha256),
            )
            doc_id = cur.fetchone()[0]
        if own:
            conn.commit()
        return doc_id
    finally:
        if own:
            conn.close()


def set_document_status(document_id: int, status: str, *, page_count=None,
                        template_id=None, run_metrics=None, error=None, conn=None):
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {SCHEMA}.chargesheet_documents
                       SET status = %s,
                           page_count = COALESCE(%s, page_count),
                           template_id = COALESCE(%s, template_id),
                           run_metrics = COALESCE(%s::jsonb, run_metrics),
                           error = %s,
                           completed_ts = CASE WHEN %s IN ('done','failed')
                                               THEN now() ELSE completed_ts END
                     WHERE document_id = %s""",
                (status, page_count, template_id,
                 json.dumps(run_metrics) if run_metrics is not None else None,
                 error, status, document_id),
            )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


# ---------- pages / extractions / notes ------------------------------------

def persist_page(document_id: int, page_result: dict, page_blob_path: str,
                 conn=None) -> int:
    """Insert one page + its extractions + notes. Returns page_id.
    Attaches extraction_ids back onto page_result items (so an API caller can
    return stable handles for 'incorrect' feedback)."""
    own = conn is None
    conn = conn or _conn()
    try:
        h = page_result.get("header", {}) or {}
        tm = page_result.get("template_match", {}) or {}
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SCHEMA}.chargesheet_pages
                        (document_id, page_number, page_blob_path, template_ok,
                         template_state, template_score, catalog_used, header,
                         patient_name, dob, flags, raw_result)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb)
                    ON CONFLICT (document_id, page_number) DO UPDATE
                        SET page_blob_path = EXCLUDED.page_blob_path,
                            header = EXCLUDED.header,
                            flags = EXCLUDED.flags,
                            raw_result = EXCLUDED.raw_result
                    RETURNING page_id""",
                (document_id, page_result.get("page"), page_blob_path,
                 _as_bool(page_result.get("template_ok")),
                 tm.get("state"), tm.get("score"), tm.get("catalog"),
                 json.dumps(h), (h.get("name") or "").strip() or None,
                 (h.get("dob") or "").strip() or None,
                 json.dumps(page_result.get("flags", [])),
                 json.dumps(page_result)),
            )
            page_id = cur.fetchone()[0]

            # clear-and-reinsert children so re-runs are clean
            cur.execute(f"DELETE FROM {SCHEMA}.chargesheet_extractions WHERE page_id=%s", (page_id,))
            cur.execute(f"DELETE FROM {SCHEMA}.chargesheet_notes       WHERE page_id=%s", (page_id,))

            def _ins_extractions(items, kind, status):
                for it in items or []:
                    cur.execute(
                        f"""INSERT INTO {SCHEMA}.chargesheet_extractions
                                (page_id, document_id, page_number, kind, status,
                                 code, description, section, mark, reason, confidence)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            RETURNING extraction_id""",
                        (page_id, document_id, page_result.get("page"), kind, status,
                         it.get("code"), it.get("description"), it.get("section"),
                         it.get("mark"), it.get("reason"), it.get("confidence")),
                    )
                    it["extraction_id"] = cur.fetchone()[0]

            _ins_extractions(page_result.get("circled_procedures"), "procedure", "confirmed")
            _ins_extractions(page_result.get("circled_diagnoses"),  "diagnosis", "confirmed")
            # possible_marks: classify by whether the code looks like an ICD-10
            for pm in page_result.get("possible_marks", []) or []:
                kind = "diagnosis" if _looks_icd10(pm.get("code", "")) else "procedure"
                cur.execute(
                    f"""INSERT INTO {SCHEMA}.chargesheet_extractions
                            (page_id, document_id, page_number, kind, status,
                             code, description, section, reason, confidence)
                        VALUES (%s,%s,%s,%s,'possible',%s,%s,%s,%s,%s)
                        RETURNING extraction_id""",
                    (page_id, document_id, page_result.get("page"), kind,
                     pm.get("code"), pm.get("description"), pm.get("section"),
                     pm.get("reason"), pm.get("confidence")),
                )
                pm["extraction_id"] = cur.fetchone()[0]

            for n in page_result.get("notes", []) or []:
                cur.execute(
                    f"""INSERT INTO {SCHEMA}.chargesheet_notes
                            (page_id, document_id, page_number, text, near,
                             off_form_code, confidence)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (page_id, document_id, page_result.get("page"),
                     n.get("text"), n.get("near"),
                     n.get("handwritten_code_not_on_form") or n.get("off_form_code"),
                     n.get("confidence")),
                )
        if own:
            conn.commit()
        return page_id
    finally:
        if own:
            conn.close()


# ---------- feedback (the two review signals) ------------------------------

def record_feedback(document_id: int, page_number: int, feedback_type: str, *,
                    extraction_id=None, target="code", code=None,
                    correct_code=None, correct_value=None, section=None,
                    mark=None, reviewer=None, note=None, conn=None) -> int:
    """
    feedback_type == 'incorrect' : what was identified is wrong (false positive).
                                   Pass extraction_id of the offending row.
    feedback_type == 'missed'    : what was NOT identified (false negative).
                                   Pass code (+ section/mark) of what should have
                                   been captured; extraction_id stays NULL.
    """
    if feedback_type not in ("incorrect", "missed"):
        raise ValueError("feedback_type must be 'incorrect' or 'missed'")
    if feedback_type == "incorrect" and extraction_id is None:
        raise ValueError("'incorrect' feedback requires extraction_id")
    if feedback_type == "missed" and not code:
        raise ValueError("'missed' feedback requires code")

    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SCHEMA}.chargesheet_feedback
                        (document_id, page_number, extraction_id, feedback_type,
                         target, code, correct_code, correct_value, section,
                         mark, reviewer, note)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING feedback_id""",
                (document_id, page_number, extraction_id, feedback_type, target,
                 code, correct_code, correct_value, section, mark, reviewer, note),
            )
            fid = cur.fetchone()[0]
        if own:
            conn.commit()
        return fid
    finally:
        if own:
            conn.close()


# ---------- reads for the API ----------------------------------------------

def get_document(document_id: int, conn=None) -> Optional[dict]:
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT document_id, practice_id, practice_name,
                           source_blob_path, pages_blob_prefix, file_name,
                           page_count, template_id, status, error, run_metrics,
                           created_ts, completed_ts
                      FROM {SCHEMA}.chargesheet_documents WHERE document_id=%s""",
                (document_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
    finally:
        if own:
            conn.close()


def get_page_results(document_id: int, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT page_id, page_number, page_blob_path, header, flags,
                           template_state, template_score
                      FROM {SCHEMA}.chargesheet_pages
                     WHERE document_id=%s ORDER BY page_number""",
                (document_id,))
            pcols = [d[0] for d in cur.description]
            pages = [dict(zip(pcols, r)) for r in cur.fetchall()]
            for p in pages:
                cur.execute(
                    f"""SELECT extraction_id, kind, status, code, description,
                               section, mark, reason, confidence
                          FROM {SCHEMA}.chargesheet_extractions
                         WHERE page_id=%s ORDER BY extraction_id""",
                    (p["page_id"],))
                ecols = [d[0] for d in cur.description]
                p["extractions"] = [dict(zip(ecols, r)) for r in cur.fetchall()]
            return pages
    finally:
        if own:
            conn.close()


# ---------- catalog store (hot-reloadable) ---------------------------------

def save_catalog(catalog: dict, conn=None):
    own = conn is None
    conn = conn or _conn()
    try:
        cell_count = sum(len(s.get("cells", [])) for s in catalog.get("sections", []))
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SCHEMA}.chargesheet_catalogs
                        (template_id, version, status, source, fingerprint,
                         header_fields, sections, cell_count)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                    ON CONFLICT (template_id, version) DO UPDATE
                        SET status = EXCLUDED.status,
                            fingerprint = EXCLUDED.fingerprint,
                            sections = EXCLUDED.sections,
                            cell_count = EXCLUDED.cell_count""",
                (catalog["template_id"], catalog.get("version", 1),
                 catalog.get("status", "active"), catalog.get("source"),
                 json.dumps(catalog["fingerprint"]),
                 json.dumps(catalog.get("header_fields", [])),
                 json.dumps(catalog["sections"]), cell_count),
            )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def load_active_catalogs(conn=None) -> list[dict]:
    """Hot-reload: every active catalog, shaped like the on-disk catalog dicts
    so fingerprint.score() / extract.build_user_prompt() work unchanged."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT template_id, version, status, source, fingerprint,
                           header_fields, sections
                      FROM {SCHEMA}.chargesheet_catalogs
                     WHERE status='active'""")
            out = []
            for tid, ver, st, src, fp, hf, secs in cur.fetchall():
                out.append({"template_id": tid, "version": ver, "status": st,
                            "source": src, "fingerprint": fp,
                            "header_fields": hf, "sections": secs})
            return out
    finally:
        if own:
            conn.close()


# ---------- tiny helpers ----------------------------------------------------

def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return None


def _looks_icd10(code: str) -> bool:
    import re
    return bool(re.match(r"^[A-TV-Z]\d", (code or "").strip()))
