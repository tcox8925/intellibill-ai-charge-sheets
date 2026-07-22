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
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict
from zoneinfo import ZoneInfo

from auth import get_kv_client, get_pg_connection, reconnect_if_stale

SCHEMA = "wpo"
EDI_TEBRA_SCHEMA = '"EDI_Tebra"'
ATTACHMENTS_TABLE = f"{EDI_TEBRA_SCHEMA}.attachments"
ATTACHMENT_CLM_LOGIN = os.environ["ATTACHMENT_CLM_LOGIN"]
ATTACHMENT_USER_ID = os.environ["ATTACHMENT_USER_ID"]
ATTACHMENT_STATUS = "G"


JSONDict = Dict[str, Any]


class ProcessedExtractionPayload(TypedDict):
    page: Optional[int]
    header: JSONDict
    circled_procedures: List[JSONDict]
    circled_diagnoses: List[JSONDict]
    possible_marks: List[JSONDict]
    notes: List[JSONDict]


class ExtractionMetadataPayload(TypedDict):
    page_blob_path: str
    page_number: Optional[int]
    template_ok: Optional[bool]
    template_match: JSONDict
    catalog_used: Optional[str]
    patient_name: Optional[str]
    dob: Optional[str]
    flags: List[Any]


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


def update_attachment(clm_att_path: str, *, new_blob_path: Optional[str] = None,
                      status: Optional[str] = None,
                      processed: Optional[bool] = None,
                      raw_extracted_data: Optional[Any] = None,
                      page_count: Optional[int] = None,
                      extracted_files_count: Optional[int] = None,
                      conn=None) -> bool:
    """Update selected attachment fields for the row identified by clm_att_path."""
    assignments = []
    params = []

    if new_blob_path is not None:
        assignments.extend([
            "clm_att_path=%s",
            "clm_att_filename=%s",
        ])
        params.extend([new_blob_path, os.path.basename(new_blob_path)])
    if status is not None:
        assignments.append("status=%s")
        params.append(status)
    if processed is not None:
        assignments.append("processed=%s")
        params.append(processed)
    if raw_extracted_data is not None:
        assignments.append("raw_extracted_data=%s::jsonb")
        params.append(json.dumps(raw_extracted_data))
    if page_count is not None:
        assignments.append("page_count=%s")
        params.append(page_count)
    if extracted_files_count is not None:
        assignments.append("extracted_files_count=%s")
        params.append(extracted_files_count)

    if not assignments:
        return False

    own = conn is None
    conn = conn or _conn()
    try:
        updated_at = _current_cst_timestamp()
        with conn.cursor() as cur:
            params.extend([updated_at, clm_att_path])
            cur.execute(
                f"""UPDATE {ATTACHMENTS_TABLE}
                       SET {', '.join(assignments)},
                           updated_at=%s
                     WHERE clm_att_path=%s""",
                params,
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


def get_attachment_by_path(clm_att_path: str, conn=None) -> Optional[dict]:
    """Return the first attachment row for the given blob path."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT *
                      FROM {ATTACHMENTS_TABLE}
                     WHERE clm_att_path=%s
                     LIMIT 1""",
                (clm_att_path,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
    finally:
        if own:
            conn.close()


def get_attachment_by_id(attachment_id: int, conn=None) -> Optional[dict]:
    """Return the attachment row for the given id."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT *
                      FROM {ATTACHMENTS_TABLE}
                     WHERE id=%s
                     LIMIT 1""",
                (attachment_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
    finally:
        if own:
            conn.close()


def get_attachment_by_sha(file_sha256: str, conn=None) -> Optional[dict]:
    """Return the first attachment row for the given SHA as a dictionary."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT *
                      FROM {ATTACHMENTS_TABLE}
                     WHERE sha=%s
                     LIMIT 1""",
                (file_sha256,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
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
                    current_attachment_path: Optional[str] = None,
                    conn=None) -> int:
    """Register a source PDF as a run. Idempotent on file_sha256: a re-ingest
    of the same bytes returns the existing document_id (resume-friendly).
    practice_id may be None if the practice wasn't found in "EDI_Tebra".practice;
    practice_name is always stored."""
    own = conn is None
    conn = conn or _conn()
    try:
        duplicate_attachment = get_attachment_by_sha(file_sha256, conn=conn)
        if (duplicate_attachment and
                duplicate_attachment.get("clm_att_path") != current_attachment_path):
            raise ValueError(
                "another file with the same sha256 is present: "
                f"id={duplicate_attachment.get('id')}, "
                f"clm_att_filename={duplicate_attachment.get('clm_att_filename')}, "
                f"clm_att_path={duplicate_attachment.get('clm_att_path')}, "
                f"attachment_type={duplicate_attachment.get('attachment_type')}, "
                f"sha={duplicate_attachment.get('sha')}"
            )
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


def persist_page_v2(document_id: int, page_result: dict, page_blob_path: str,
                    conn=None) -> int:
    """Insert one extracted page as a child attachment row."""
    own = conn is None
    conn = conn or _conn()
    try:
        parent_attachment = get_attachment_by_id(document_id, conn=conn)
        if not parent_attachment:
            raise ValueError(f"parent attachment not found: id={document_id}")
        processed_payload = _build_processed_payload(page_result)
        metadata_payload = _build_extraction_metadata_payload(
            page_result, page_blob_path)
        page_file_name = os.path.basename(page_blob_path)
        original_file_name = page_file_name.rsplit("_", 1)[-1]
        att_datetime = _current_cst_timestamp()
        created_at = att_datetime
        updated_at = att_datetime
        with conn.cursor() as cur:
            cur.execute(
                f"""DELETE FROM {ATTACHMENTS_TABLE}
                     WHERE parent_attachment_id=%s
                       AND clm_att_filename=%s""",
                (document_id, page_file_name),
            )
            cur.execute(
                f"""INSERT INTO {ATTACHMENTS_TABLE}
                        (type_id, clm_att_path, clm_att_filename,
                         clm_att_datetime, clm_login, created_at, updated_at,
                         attachment_type, parent_attachment_id,
                         parent_attachment_name, original_file_name,
                         client_id, group_id, practice_id,
                         page_count, extracted_files_count,
                         user_id, status,
                         raw_extracted_data, processed_extracted_data,
                         extraction_metadata, processed, sha)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                        %s, %s)
                    RETURNING id""",
                (
                    None,
                    page_blob_path,
                    page_file_name,
                    att_datetime,
                    ATTACHMENT_CLM_LOGIN,
                    created_at,
                    updated_at,
                    parent_attachment.get("attachment_type"),
                    document_id,
                    parent_attachment.get("clm_att_filename"),
                    original_file_name,
                    parent_attachment.get("client_id"),
                    parent_attachment.get("group_id"),
                    parent_attachment.get("practice_id"),
                    1,
                    1,
                    ATTACHMENT_USER_ID,
                    ATTACHMENT_STATUS,
                    json.dumps(page_result),
                    json.dumps(processed_payload),
                    json.dumps(metadata_payload),
                    True,
                    None,
                ),
            )
            row = cur.fetchone()
        if own:
            conn.commit()
        return row[0]
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
                f"""SELECT id, clm_att_path, clm_att_filename, attachment_type,
                           parent_attachment_id, processed, status,
                           extraction_metadata, created_at, updated_at
                      FROM {ATTACHMENTS_TABLE}
                     WHERE id=%s
                     LIMIT 1""",
                (document_id,))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                attachment = dict(zip(cols, row))
                cur.execute(
                    f"""SELECT COUNT(*)
                          FROM {ATTACHMENTS_TABLE}
                         WHERE parent_attachment_id=%s""",
                    (document_id,))
                child_page_count = cur.fetchone()[0]
                return {
                    "document_id": attachment["id"],
                    "source_blob_path": attachment.get("clm_att_path"),
                    "pages_blob_prefix": _pages_blob_prefix_for_path(
                        attachment.get("clm_att_path")),
                    "file_name": attachment.get("clm_att_filename"),
                    "status": _attachment_status(
                        bool(attachment.get("processed")), child_page_count),
                    "attachment_type": attachment.get("attachment_type"),
                    "parent_attachment_id": attachment.get("parent_attachment_id"),
                    "processed": bool(attachment.get("processed")),
                    "child_page_count": child_page_count,
                    "run_metrics": None,
                    "error": None,
                    "created_ts": attachment.get("created_at"),
                    "completed_ts": attachment.get("updated_at") if attachment.get("processed") else None,
                    "extraction_metadata": attachment.get("extraction_metadata"),
                }
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
                f"""SELECT 1
                      FROM {ATTACHMENTS_TABLE}
                     WHERE id=%s
                     LIMIT 1""",
                (document_id,))
            attachment_exists = cur.fetchone() is not None
            cur.execute(
                f"""SELECT id, clm_att_path, clm_att_filename,
                           raw_extracted_data, processed_extracted_data,
                           extraction_metadata, attachment_type,
                           created_at, updated_at
                      FROM {ATTACHMENTS_TABLE}
                     WHERE parent_attachment_id=%s
                     ORDER BY COALESCE((extraction_metadata->>'page_number')::int, 0),
                              id""",
                (document_id,))
            child_rows = cur.fetchall()
            if child_rows:
                cols = [d[0] for d in cur.description]
                pages = []
                for row in child_rows:
                    child = dict(zip(cols, row))
                    raw_result = child.get("raw_extracted_data") or {}
                    processed_result = child.get("processed_extracted_data") or {}
                    metadata = child.get("extraction_metadata") or {}
                    template_match = metadata.get("template_match", {}) or {}
                    pages.append({
                        "page_id": child["id"],
                        "page_number": metadata.get("page_number") or raw_result.get("page") or processed_result.get("page"),
                        "page_blob_path": child.get("clm_att_path"),
                        "header": processed_result.get("header") or raw_result.get("header") or {},
                        "flags": metadata.get("flags", []),
                        "template_state": template_match.get("state"),
                        "template_score": template_match.get("score"),
                        "template_ok": metadata.get("template_ok"),
                        "raw_result": raw_result,
                        "processed_result": processed_result,
                        "extractions": _attachment_extractions(processed_result),
                        "notes": processed_result.get("notes", []),
                    })
                return pages
            if attachment_exists:
                return []
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


def _current_cst_timestamp() -> str:
    return datetime.now(ZoneInfo("America/Chicago")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _build_processed_payload(page_result: dict) -> ProcessedExtractionPayload:
    return {
        "page": page_result.get("page"),
        "header": page_result.get("header", {}) or {},
        "circled_procedures": page_result.get("circled_procedures", []),
        "circled_diagnoses": page_result.get("circled_diagnoses", []),
        "possible_marks": page_result.get("possible_marks", []),
        "notes": page_result.get("notes", []),
    }


def _build_extraction_metadata_payload(
        page_result: dict,
        page_blob_path: str) -> ExtractionMetadataPayload:
    header = page_result.get("header", {}) or {}
    template_match = page_result.get("template_match", {}) or {}
    return {
        "page_blob_path": page_blob_path,
        "page_number": page_result.get("page"),
        "template_ok": _as_bool(page_result.get("template_ok")),
        "template_match": template_match,
        "catalog_used": template_match.get("catalog"),
        "patient_name": (header.get("name") or "").strip() or None,
        "dob": (header.get("dob") or "").strip() or None,
        "flags": page_result.get("flags", []),
    }


def _attachment_status(processed: bool, child_page_count: int) -> str:
    if processed:
        return "done"
    if child_page_count > 0:
        return "processing"
    return "queued"


def _pages_blob_prefix_for_path(clm_att_path: Optional[str]) -> Optional[str]:
    if not clm_att_path:
        return None
    parent = os.path.dirname(clm_att_path).strip("/")
    stem = os.path.splitext(os.path.basename(clm_att_path))[0]
    if parent:
        return f"{parent}/{stem}-"
    return f"{stem}-"


def _attachment_extractions(processed_result: dict) -> list[dict]:
    extractions = []
    for item in processed_result.get("circled_procedures", []) or []:
        extractions.append({
            "kind": "procedure",
            "status": "confirmed",
            "code": item.get("code"),
            "description": item.get("description"),
            "section": item.get("section"),
            "mark": item.get("mark"),
            "reason": item.get("reason"),
            "confidence": item.get("confidence"),
        })
    for item in processed_result.get("circled_diagnoses", []) or []:
        extractions.append({
            "kind": "diagnosis",
            "status": "confirmed",
            "code": item.get("code"),
            "description": item.get("description"),
            "section": item.get("section"),
            "mark": item.get("mark"),
            "reason": item.get("reason"),
            "confidence": item.get("confidence"),
        })
    for item in processed_result.get("possible_marks", []) or []:
        extractions.append({
            "kind": "diagnosis" if _looks_icd10(item.get("code", "")) else "procedure",
            "status": "possible",
            "code": item.get("code"),
            "description": item.get("description"),
            "section": item.get("section"),
            "mark": item.get("mark"),
            "reason": item.get("reason"),
            "confidence": item.get("confidence"),
        })
    return extractions


def _looks_icd10(code: str) -> bool:
    import re
    return bool(re.match(r"^[A-TV-Z]\d", (code or "").strip()))
