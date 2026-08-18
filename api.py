"""
api.py — FastAPI service for the charge-sheet pipeline (kept SEPARATE from the
run.py CLI). It wires the extraction core to blob storage (storage.py) and
Postgres (db.py). PDF billing-code selection goes through the locked-template
computer-vision pipeline (v2_2_1_computer_vision.pipeline_adapter.process_pdf,
same call shape as run.process_pdf) rather than the LLM. Single-image blobs
are no longer supported (neither CV pipeline has a single-image entry point)
and are rejected with a 400 at ingest time instead of being processed.

Endpoints
    GET  /health                                      simple service health check
    GET  /health/listattachments                      list the first 10 attachment rows
    POST /chargesheet/extract                         process one blob path synchronously
    GET  /chargesheet/folders                         list container folders
    POST /chargesheet/ingest                          {filename?} -> document_id
    POST /chargesheet/ingest-custom-list              {blob_paths[]} -> per-path results
    POST /chargesheet/ingest-all                      queue all supported claim files
    GET  /chargesheet/documents/{document_id}         status + metrics
    GET  /chargesheet/documents/{document_id}/pages   per-page results + extraction_ids
    POST /chargesheet/feedback                         the two review signals
    GET  /chargesheet/documents/{document_id}/feedback list feedback
    POST /external/auth/login                         authenticate and return auth cookies
    POST /external/claims/create-prof-claim           queue claim creation via tRPC
    POST /external/claims/create-prof-claim-batch     queue claims for a list of attachment_ids
    POST /external/claims/create-prof-claim-all       sweep unclaimed 'G'-status children
    POST /chargesheet/archive-processed               archive top-level processed, unarchived attachments

Flow of POST /ingest (async):
    find source folder -> find newest .pdf -> download bytes -> register a
    document row (idempotent on sha256) -> BackgroundTask:
        split PDF -> per page: extract -> upload page-NN.png to
        {source-folder}/pages/{stem}/ -> persist page+extractions+notes to Postgres
    status walks queued -> processing -> done|failed.

Run:  uvicorn api:app --host 0.0.0.0 --port 8100
Auth is the same KV/VNet story as pch-eob-pipeline (via run.make_client / db).
"""

import os
import tempfile
import logging
from collections import Counter
from datetime import date
from typing import Dict, List
from typing import Optional, Union

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, model_validator

import auth
import storage
import db
import external_apis
import image_ocr
import run
from v2_2_1_computer_vision import pipeline_adapter as cv_pipeline

app = FastAPI(title="834 Charge-sheet OCR", version="1.0")

chargesheet_logger = logging.getLogger("chargesheet")
if not chargesheet_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    chargesheet_logger.addHandler(handler)
chargesheet_logger.setLevel(logging.INFO)
chargesheet_logger.propagate = False

logger = logging.getLogger("chargesheet.api")

INGEST_ALL_EXCLUDE = [
    "Archive",
    "Partner Integrations",
    "check_attachments",
    "claim_attachments",
    "medical-extraction",
    "ml-models",
    "patient_attachments",
    "users_logo",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        os.environ.get("FRONTEND_URL"),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Toggle for whether ingest automatically calls create-claim on each child
# attachment produced during extraction. Set AUTO_CREATE_CLAIMS=false to
# extract without ever touching the external claims API.
AUTO_CREATE_CLAIMS = os.environ.get("AUTO_CREATE_CLAIMS", "true").strip().lower() in (
    "1", "true", "yes", "y", "on",
)


def _client():
    # one client per process; cheap to rebuild if needed
    if not hasattr(_client, "_c"):
        _client._c = run.make_client()
    return _client._c


def _registry():
    """Hot-reloadable catalog registry: active catalogs from Postgres, with the
    on-disk catalogs as a fallback for a cold DB."""
    cats = db.load_active_catalogs()
    if cats:
        return [(c["template_id"], c) for c in cats]
    return run.load_registry()


# ---------- models ----------------------------------------------------------

class IngestRequest(BaseModel):
    # render DPI is fixed and callers may pass either an exact filename at the
    # configured root or a full blob path.
    filename: Optional[str] = None
    blob_path: Optional[str] = None


class IngestCustomListRequest(BaseModel):
    blob_paths: List[str]


class IngestAllRequest(BaseModel):
    include_archive: bool = False


class IngestAllResult(BaseModel):
    queued: List[Dict[str, object]]
    skipped: List["IngestSkipResult"]
    missed_files: List["IngestSkipResult"]
    excluded_folders: List[str]


class IngestSkipResult(BaseModel):
    # Shared skip payload for direct ingest and ingest-all. Skip responses stay
    # blob/folder scoped and do not include resolved practice metadata.
    status: str = "skipped"
    blob_path: Optional[str] = None
    reason: str
    folder: Optional[str] = None


class FeedbackRequest(BaseModel):
    document_id: int
    page_number: int
    feedback_type: str                 # 'incorrect' | 'missed'
    target: str = "code"               # code | procedure | diagnosis | note | header
    extraction_id: Optional[int] = None   # required for 'incorrect'
    code: Optional[str] = None            # required for 'missed'
    correct_code: Optional[str] = None
    correct_value: Optional[str] = None
    section: Optional[str] = None
    mark: Optional[str] = None
    reviewer: Optional[str] = None
    note: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if self.feedback_type not in ("incorrect", "missed"):
            raise ValueError("feedback_type must be 'incorrect' or 'missed'")
        if self.feedback_type == "incorrect" and self.extraction_id is None:
            raise ValueError("'incorrect' feedback requires extraction_id "
                             "(what was identified is wrong)")
        if self.feedback_type == "missed" and not self.code:
            raise ValueError("'missed' feedback requires code "
                             "(what was NOT identified)")
        return self


class ExternalClaimRequest(BaseModel):
    attachment_id: int
    run_async: bool = True
    cookie_header: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if not self.cookie_header:
            return self
        return self


class ExternalClaimBatchRequest(BaseModel):
    attachment_ids: List[int]
    run_async: bool = True
    cookie_header: Optional[str] = None
    limit: Optional[int] = None


class ExternalClaimAllRequest(BaseModel):
    run_async: bool = True
    cookie_header: Optional[str] = None
    limit: Optional[int] = None


class ArchiveProcessedRequest(BaseModel):
    execute: bool = False
    limit: Optional[int] = None


# ---------- endpoints -------------------------------------------------------

# curl -s http://localhost:8100/health | jq
@app.get("/health")
def health():
    db_status = auth.check_db_connection()
    storage_status = storage.check_blob_connection()
    connected = db_status.get("connected") and storage_status.get("connected")
    return {
        "status": "ok" if connected else "degraded",
        "message": "hello my world",
        "database": db_status,
        "storage": storage_status,
    }


# curl -s http://localhost:8100/health/listattachments | jq
@app.get("/health/listattachments")
def health_listattachments():
    return {"attachments": db.list_attachment_entries()}


# curl -s -X POST http://localhost:8100/chargesheet/extract \
#   -H 'content-type: application/json' \
#   -d '{"blob_path": "Folder/SomeFile.pdf"}' | jq
@app.post("/chargesheet/extract")
def extract_blob_sync(req: IngestRequest):
    blob_path = _resolve_ingest_target(req)
    if is_processed(blob_path):
        logger.info("Skipping already processed claim file: %s", blob_path)
        return _build_skip_result(blob_path, "already_processed")

    return _handle_extract_blob_sync(blob_path)

# curl -s http://localhost:8100/chargesheet/folders | jq
@app.get("/chargesheet/folders")
def folders():
    return {"container": storage.CONTAINER, "folders": storage.list_folders()}


# curl -s -X POST http://localhost:8100/chargesheet/ingest \
#   -H 'content-type: application/json' \
#   -d '{"blob_path": "Folder/SomeFile.pdf"}' | jq
@app.post("/chargesheet/ingest")
def ingest(req: IngestRequest, bg: BackgroundTasks) -> Union[Dict[str, object], IngestSkipResult]:
    blob_path = _resolve_ingest_target(req)
    if is_processed(blob_path):
        logger.info("Skipping already processed claim file: %s", blob_path)
        return _build_skip_result(blob_path, "already_processed")

    result = _handle_ingest_blob(blob_path, bg)
    if result is None:
        return _build_skip_result(blob_path,
                                  "unsupported_file_marked_processed")
    return result


# curl -s -X POST http://localhost:8100/chargesheet/ingest-custom-list \
#   -H 'content-type: application/json' \
#   -d '{"blob_paths": ["Folder/a.pdf", "Folder/b.pdf"]}' | jq
@app.post("/chargesheet/ingest-custom-list")
def ingest_custom_list(req: IngestCustomListRequest,
                       bg: BackgroundTasks) -> List[Union[Dict[str, object], IngestSkipResult]]:
    results = []
    for blob_path in req.blob_paths:
        blob_path = blob_path.strip()
        if not blob_path:
            results.append(_build_skip_result(blob_path, "blank_blob_path"))
            continue

        try:
            if is_processed(blob_path):
                logger.info("Skipping already processed claim file: %s", blob_path)
                results.append(_build_skip_result(blob_path, "already_processed"))
                continue

            result = _handle_ingest_blob(blob_path, bg)
            if result is None:
                results.append(_build_skip_result(
                    blob_path, "unsupported_file_marked_processed"))
                continue
            results.append(result)
        except HTTPException as exc:
            results.append(_build_skip_result(blob_path, f"error: {exc.detail}"))
    return results


# curl -s -X POST http://localhost:8100/chargesheet/ingest-all | jq
# curl -s -X POST http://localhost:8100/chargesheet/ingest-all \
#   -H 'content-type: application/json' \
#   -d '{"include_archive": true}' | jq
@app.post("/chargesheet/ingest-all")
def ingest_all(bg: BackgroundTasks,
               req: IngestAllRequest = IngestAllRequest()) -> IngestAllResult:
    queued = []
    skipped = []
    missed_files = []

    if req.include_archive:
        for blob_path in storage.list_archive_claim_files():
            _ingest_claim_file(
                blob_path, _archive_entity_folder(blob_path), bg,
                queued, skipped, missed_files)
        return IngestAllResult(
            queued=queued,
            skipped=skipped,
            missed_files=missed_files,
            excluded_folders=[],
        )

    folder_payload = folders()
    for folder_name in folder_payload["folders"]:
        if folder_name in INGEST_ALL_EXCLUDE:
            skipped.append({"folder": folder_name, "reason": "excluded"})
            continue
        if not storage.is_entity_group_folder(folder_name):
            skipped.append({"folder": folder_name, "reason": "not_entity_group_folder"})
            continue

        claim_files = storage.list_claim_files(folder_name)
        if not claim_files:
            skipped.append({"folder": folder_name, "reason": "no_claim_files"})
            continue

        for blob_path in claim_files:
            _ingest_claim_file(blob_path, folder_name, bg, queued, skipped, missed_files)

    return IngestAllResult(
        queued=queued,
        skipped=skipped,
        missed_files=missed_files,
        excluded_folders=INGEST_ALL_EXCLUDE,
    )


def _archive_entity_folder(blob_path: str) -> str:
    """Archive/{entity}/Claims/{date}/file.ext -> {entity}"""
    parts = blob_path.strip("/").split("/")
    return parts[1] if len(parts) > 1 else "Archive"


def _ingest_claim_file(blob_path: str, folder_name: str, bg: BackgroundTasks,
                       queued: list, skipped: list, missed_files: list) -> None:
    if is_processed(blob_path):
        logger.info("Skipping already processed claim file: %s", blob_path)
        skipped.append(_build_skip_result(
            blob_path, "already_processed", folder=folder_name))
        return

    try:
        queue_result = _handle_ingest_blob(blob_path, bg)
    except HTTPException as exc:
        if exc.status_code == 404 and str(exc.detail).startswith(
                "attachment not found for blob path:"):
            logger.warning("Missing attachment row for blob path: %s", blob_path)
            missed_files.append(_build_skip_result(
                blob_path, "attachment_not_found", folder=folder_name))
            return
        raise
    if queue_result is None:
        skipped.append(_build_skip_result(
            blob_path, "unsupported_file_marked_processed", folder=folder_name))
        return
    queued.append(queue_result)


def is_processed(blob_path: str) -> bool:
    return db.is_attachment_processed(blob_path)


def construct_archive_folder_path(blob_path: str) -> str:
    path_parts = [part for part in blob_path.strip("/").split("/") if part]
    entity_group = path_parts[1] if path_parts[0] == "Archive" else path_parts[0]
    current_date = date.today().isoformat()
    return f"Archive/{entity_group}/Claims/{current_date}"


def _archive_blob_path(blob_path: str, archive_folder_path: str, *,
                       status: Optional[str] = None,
                       processed: Optional[bool] = True,
                       raw_extracted_data=None,
                       page_count: Optional[int] = None,
                       extracted_files_count: Optional[int] = None) -> dict:
    attachment = db.get_attachment_by_path(blob_path)
    if not attachment:
        raise HTTPException(404, f"attachment not found for blob path: {blob_path}")

    target_blob_path = f"{archive_folder_path.strip().strip('/')}/{os.path.basename(blob_path)}"
    updated = db.update_attachment(
        blob_path,
        new_blob_path=target_blob_path,
        status=status,
        processed=processed,
        raw_extracted_data=raw_extracted_data,
        page_count=page_count,
        extracted_files_count=extracted_files_count,
    )
    if not updated:
        raise HTTPException(500, "attachment path update failed")

    try:
        storage.move_blob(blob_path, target_blob_path)
    except Exception as exc:
        logger.exception(
            "storage.move_blob failed for %s -> %s; rolling back attachment update",
            blob_path, target_blob_path,
        )
        rollback_updated = db.update_attachment(
            target_blob_path,
            new_blob_path=blob_path,
            status=attachment.get("status"),
            processed=attachment.get("processed"),
            raw_extracted_data=attachment.get("raw_extracted_data"),
            page_count=attachment.get("page_count"),
            extracted_files_count=attachment.get("extracted_files_count"),
            rotation_degrees=attachment.get("rotation_degrees"),
        )
        if not rollback_updated:
            raise HTTPException(
                500,
                "blob move failed after attachment update and rollback failed",
            ) from exc
        raise HTTPException(
            500,
            "blob move failed after attachment update; database changes were rolled back",
        ) from exc

    return {
        "status": "archived",
        "old_blob_path": blob_path,
        "new_blob_path": target_blob_path,
        "attachment_id": attachment["id"],
        "attachment_name": os.path.basename(target_blob_path),
        "attachment_status": status,
        "processed": processed,
        "page_count": page_count,
        "extracted_files_count": extracted_files_count,
    }


def finalize_processed_attachment(blob_path: str, document_id: int, results,
                                  page_count: int):
    rotation_degrees = _mode_applied_rotation(results)
    updated = db.update_attachment(
        blob_path,
        status="C",
        processed=True,
        raw_extracted_data=results,
        page_count=page_count,
        extracted_files_count=page_count,
        rotation_degrees=rotation_degrees,
    )
    if not updated:
        raise HTTPException(500, "attachment finalization update failed")

    result = {
        "status": "processed",
        "blob_path": blob_path,
        "attachment_name": os.path.basename(blob_path),
        "processed": True,
        "page_count": page_count,
        "extracted_files_count": page_count,
        "rotation_degrees": rotation_degrees,
    }
    attachment = db.get_attachment_by_path(blob_path)
    if attachment:
        result["attachment_id"] = attachment["id"]
    result["document_id"] = document_id
    return result


def _mode_applied_rotation(results) -> Optional[int]:
    rotations = []
    for result in results or []:
        orientation = result.get("orientation") or {}
        rotation = orientation.get("applied_rotation_deg")
        if rotation is None:
            continue
        try:
            rotations.append(int(rotation))
        except (TypeError, ValueError):
            continue
    if not rotations:
        return None
    return Counter(rotations).most_common(1)[0][0]


def archive_for_processing(blob_path: str) -> dict:
    return _archive_blob_path(
        blob_path,
        construct_archive_folder_path(blob_path),
        processed=False,
    )


def _build_skip_result(blob_path: Optional[str], reason: str,
                       folder: Optional[str] = None) -> IngestSkipResult:
    return IngestSkipResult(
        blob_path=blob_path,
        reason=reason,
        folder=folder,
    )


def _resolve_ingest_target(req: IngestRequest) -> str:
    if req.blob_path:
        blob_path = req.blob_path.strip()
        if not blob_path:
            raise HTTPException(400, "blob_path cannot be blank")
        return blob_path

    src = storage.find_source_pdf(req.filename)
    if not src:
        detail = "no source PDF found"
        if req.filename:
            detail += f" matching {req.filename}"
        raise HTTPException(404, detail)
    return src


def _mark_unsupported_processed(blob_path: str):
    db.update_attachment(blob_path, processed=True, status="E")
    logger.info("Auto-marked unsupported claim file as errored: %s",
                blob_path)


def _reject_single_image(blob_path: str):
    # Single-image ingestion is no longer supported: neither CV pipeline
    # (v1_computer_vision/v2_computer_vision/v2_2_1_computer_vision) has a single-image entry point,
    # only process_pdf(). Rather than silently mark these processed like a
    # genuinely unsupported file type, fail loudly so a caller/integration
    # still sending image blobs notices immediately.
    if storage.is_image_path(blob_path):
        raise HTTPException(
            400,
            f"single-image ingestion is no longer supported (blob_path={blob_path}); "
            "only PDF blobs can be ingested",
        )


def _handle_ingest_blob(blob_path: str, bg: BackgroundTasks) -> Optional[dict]:
    _reject_single_image(blob_path)
    if storage.is_pdf_path(blob_path):
        original_blob_path = blob_path
        archive_result = archive_for_processing(blob_path)
        return _queue_blob_ingest(
            archive_result["new_blob_path"],
            bg,
            original_blob_path=original_blob_path,
        )

    _mark_unsupported_processed(blob_path)
    return None


def _handle_extract_blob_sync(blob_path: str) -> Union[dict, IngestSkipResult]:
    _reject_single_image(blob_path)
    if storage.is_pdf_path(blob_path):
        original_blob_path = blob_path
        archive_result = archive_for_processing(blob_path)
        return _run_blob_ingest_sync(
            archive_result["new_blob_path"],
            original_blob_path=original_blob_path,
        )

    _mark_unsupported_processed(blob_path)
    return _build_skip_result(blob_path, "unsupported_file_marked_processed")


def _blob_folder(blob_path: str) -> str:
    return blob_path.split("/", 1)[0]


def _parse_pages(pages: Optional[str]):
    try:
        return {int(x) for x in pages.split(",") if x.strip()} if pages else None
    except ValueError:
        raise HTTPException(400, "pages must be a comma-separated 1-based page list")


def _resolve_ingest_context(blob_path: str) -> dict:
    attachment = db.get_attachment_by_path(blob_path)
    if not attachment:
        raise HTTPException(404, f"attachment not found for blob path: {blob_path}")

    return {
        "attachment_id": attachment["id"],
        "attachment_name": attachment.get("clm_att_filename"),
        "attachment_type": attachment.get("attachment_type"),
        "stem": os.path.splitext(os.path.basename(blob_path))[0],
        "folder": _blob_folder(blob_path),
        "source_type": "pdf" if storage.is_pdf_path(blob_path) else "image",
    }


def _run_blob_ingest_sync(blob_path: str, *,
                          original_blob_path: Optional[str] = None,
                          want=None) -> dict:
    _reject_single_image(blob_path)
    if not storage.is_pdf_path(blob_path):
        raise HTTPException(400, "blob_path must point to a PDF")

    context = _resolve_ingest_context(blob_path)
    return _process(
        context["attachment_id"],
        context["stem"],
        blob_path,
        original_blob_path=original_blob_path or blob_path,
        want=want,
    )


def _start_external_claim_session() -> dict:
    try:
        session = external_apis.login()
        session["login_successful"] = True
        session["login_error"] = None
        return session
    except Exception as exc:
        # Never let a login failure (auth error, network hiccup, timeout,
        # anything) escape here — this runs inline in the per-page extraction
        # loop, and an uncaught exception would abort the whole document's
        # processing before the parent attachment gets finalized.
        logger.warning("External login failed: %s", exc)
        return {
            "cookie_header": None,
            "user_id": None,
            "login_successful": False,
            "login_error": str(exc),
        }


def _queue_external_claim_for_child(child_attachment_id: int,
                                    external_session: dict,
                                    parent_attachment_id: int):
    # This runs inline in the per-page extraction loop (on_page). Nothing in
    # here — including the DB write recording the outcome — may ever raise,
    # or it aborts the rest of the document's processing before the parent
    # attachment gets finalized.
    try:
        if not external_session.get("login_successful"):
            db.record_claim_creation_response(
                child_attachment_id,
                {"error": f"login failed: {external_session.get('login_error')}"},
            )
            return

        cookie_header = external_session.get("cookie_header")
        if not cookie_header:
            db.record_claim_creation_response(
                child_attachment_id, {"error": "login succeeded but no cookie_header"})
            return

        try:
            payload = external_apis.queue_claim_creation(
                cookie_header=cookie_header,
                attachment_id=child_attachment_id,
                run_async=True,
            )
            db.record_claim_creation_response(child_attachment_id, payload)
        except Exception as exc:
            logger.warning(
                "External claim creation failed for child attachment %s: %s",
                child_attachment_id,
                exc,
            )
            db.record_claim_creation_response(child_attachment_id, {"error": str(exc)})
    except Exception as exc:
        logger.exception(
            "Unexpected failure recording claim-creation outcome for child "
            "attachment %s — continuing without aborting page processing",
            child_attachment_id,
        )
        try:
            db.record_claim_creation_response(
                parent_attachment_id,
                {"error": str(exc), "child_attachment_id": child_attachment_id},
            )
        except Exception:
            logger.exception(
                "Failed to record claim-creation error on parent attachment %s",
                parent_attachment_id,
            )


def _queue_blob_ingest(archived_blob_path: str, bg: BackgroundTasks,
                       *, original_blob_path: Optional[str] = None) -> dict:
    context = _resolve_ingest_context(archived_blob_path)
    original_blob_path = original_blob_path or archived_blob_path

    logger.info(
        "Queueing %s claim file for processing: archived=%s original=%s "
        "(pages upload alongside original_blob_path, not archived_blob_path)",
        context["source_type"], archived_blob_path, original_blob_path,
    )
    bg.add_task(
        _process,
        context["attachment_id"],
        context["stem"],
        archived_blob_path,
        original_blob_path=original_blob_path,
    )
    return {
        "document_id": context["attachment_id"],
        "attachment_name": context["attachment_name"],
        "status": "queued",
        "folder": context["folder"],
        "source_blob_path": f"{storage.CONTAINER}/{archived_blob_path}",
        "pages_blob_prefix": (
            f"{storage.CONTAINER}/{storage.pages_prefix(original_blob_path)}"
        ),
        "source_type": context["source_type"],
    }


def _process(attachment_id: int, stem: str,
             blob_path: str, *, original_blob_path: Optional[str] = None,
             want=None):
    """Background worker: split -> per-page extract -> upload page -> persist.

    Code selection uses the locked-template computer-vision pipeline
    (v2_2_1_computer_vision.pipeline_adapter), not the LLM, per the swap to
    deterministic geometric detection. registry is accepted for call-shape
    compatibility but unused by that pipeline (single locked template)."""
    from PIL import Image
    registry = _registry()
    folder = _blob_folder(blob_path)
    document_id = attachment_id
    archived_blob_path = blob_path
    original_blob_path = original_blob_path or archived_blob_path
    try:
        external_session = _start_external_claim_session() if AUTO_CREATE_CLAIMS else None
        pdf_bytes = storage.download_blob(archived_blob_path)
        parent_attachment = db.get_attachment_by_id(document_id)
        if not parent_attachment:
            raise HTTPException(404, f"attachment not found: id={document_id}")
        attachment_name = parent_attachment.get("clm_att_filename")
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "src.pdf")
            with open(pdf_path, "wb") as f:
                f.write(pdf_bytes)

            def on_page(page_no, page_image_path, result):
                # upload the rendered page, then persist page+children with paths
                uploaded_blob_path = storage.upload_page(
                    original_blob_path, page_no, Image.open(page_image_path))
                result["template_match"] = result.get("template_match", {})
                child_attachment_id = db.persist_page_v2(
                    document_id,
                    result,
                    page_blob_path=uploaded_blob_path,
                )
                if AUTO_CREATE_CLAIMS:
                    _queue_external_claim_for_child(
                        child_attachment_id,
                        external_session,
                        document_id,
                    )

            results, metrics = cv_pipeline.process_pdf(
                pdf_path, _client(), registry,
                pages_dir=os.path.join(tmp, "pages"), want=want, on_page=on_page)

        tid = next((r.get("template_match", {}).get("template_id")
                    for r in results if r.get("template_match", {}).get("template_id")), None)
        _finalize_processed_blob(
            document_id, archived_blob_path, len(results), tid, metrics, results)
        return _build_processed_response(
            document_id, attachment_name, folder, archived_blob_path,
            original_blob_path, "pdf", results, metrics)
    except Exception as e:
        _mark_processing_failed(document_id, e)
        raise


# Truncated: this used to be the background worker for single-image blobs
# (image_ocr.py, LLM-based). Retired because neither CV pipeline
# (v1_computer_vision/v2_computer_vision/v2_2_1_computer_vision) has a single-image entry point —
# only process_pdf(). Single images are now rejected at ingest time by
# _reject_single_image() before a background task is ever queued, so this
# should be unreachable; it's kept as a stub (rather than deleted) so a stray
# direct call still fails loudly instead of silently doing nothing.
def _process_image_blob(*args, **kwargs):
    raise RuntimeError(
        "_process_image_blob is retired: single-image ingestion is no longer "
        "supported by either CV pipeline. This should be unreachable — "
        "single-image blobs are rejected earlier, at ingest dispatch time."
    )


def _finalize_processed_blob(document_id: int, blob_path: str, page_count: int,
                             template_id: Optional[str], run_metrics: dict,
                             results):
    try:
        db.set_document_status(document_id, "done", page_count=page_count,
                               template_id=template_id, run_metrics=run_metrics)
    except Exception as exc:
        logger.warning("Skipping legacy document status update for %s: %s",
                       document_id, exc)
    finalize_processed_attachment(blob_path, document_id, results, page_count)


def _mark_processing_failed(document_id: int, error: Exception):
    try:
        db.set_document_status(document_id, "failed", error=str(error))
    except Exception as exc:
        logger.warning("Skipping legacy document failure update for %s: %s",
                       document_id, exc)


def _build_processed_response(document_id: int, attachment_name: Optional[str],
                              folder: str, blob_path: str,
                              original_blob_path: str, source_type: str,
                              pages: list, metrics: dict) -> dict:
    return {
        "document_id": document_id,
        "attachment_name": attachment_name,
        "status": "done",
        "folder": folder,
        "source_blob_path": f"{storage.CONTAINER}/{blob_path}",
        "pages_blob_prefix": (
            f"{storage.CONTAINER}/{storage.pages_prefix(original_blob_path)}"
        ),
        "source_type": source_type,
        "page_count": len(pages),
        "pages": pages,
        "metrics": metrics,
    }


# curl -s http://localhost:8100/chargesheet/documents/123 | jq
@app.get("/chargesheet/documents/{document_id}")
def document(document_id: int):
    doc = db.get_document(document_id)
    if not doc:
        raise HTTPException(404, "document not found")
    return doc


# curl -s http://localhost:8100/chargesheet/documents/123/pages | jq
@app.get("/chargesheet/documents/{document_id}/pages")
def document_pages(document_id: int):
    if not db.get_document(document_id):
        raise HTTPException(404, "document not found")
    return {"document_id": document_id, "pages": db.get_page_results(document_id)}


# curl -s -X POST http://localhost:8100/chargesheet/feedback \
#   -H 'content-type: application/json' \
#   -d '{"document_id": 123, "page_number": 1, "feedback_type": "missed", "code": "99213"}' | jq
@app.post("/chargesheet/feedback")
def feedback(req: FeedbackRequest):
    fid = db.record_feedback(
        document_id=req.document_id, page_number=req.page_number,
        feedback_type=req.feedback_type, target=req.target,
        extraction_id=req.extraction_id, code=req.code,
        correct_code=req.correct_code, correct_value=req.correct_value,
        section=req.section, mark=req.mark, reviewer=req.reviewer, note=req.note)
    return {"feedback_id": fid, "feedback_type": req.feedback_type}


# curl -s http://localhost:8100/chargesheet/documents/123/feedback | jq
@app.get("/chargesheet/documents/{document_id}/feedback")
def list_feedback(document_id: int):
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT feedback_id, page_number, extraction_id, feedback_type,
                           target, code, correct_code, correct_value, section,
                           mark, reviewer, note, applied, created_ts
                      FROM {db.SCHEMA}.chargesheet_feedback
                     WHERE document_id=%s ORDER BY page_number, feedback_id""",
                (document_id,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"document_id": document_id, "feedback": rows}
    finally:
        conn.close()


# curl -s -X POST http://localhost:8100/external/auth/login | jq
@app.post("/external/auth/login")
def external_login():
    try:
        session = external_apis.login()
    except external_apis.ExternalApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    return session


# curl -s -X POST http://localhost:8100/external/claims/create-prof-claim \
#   -H 'content-type: application/json' \
#   -d '{"attachment_id": 123, "run_async": true}' | jq
@app.post("/external/claims/create-prof-claim")
def external_create_prof_claim(req: ExternalClaimRequest):
    try:
        cookie_header = req.cookie_header
        if not cookie_header:
            session = external_apis.login()
            cookie_header = session["cookie_header"]
        payload = external_apis.queue_claim_creation(
            cookie_header=cookie_header,
            attachment_id=req.attachment_id,
            run_async=req.run_async,
        )
    except external_apis.ExternalApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "attachment_id": req.attachment_id,
        "run_async": req.run_async,
        "result": payload,
    }


def _login_cookie_header(cookie_header: Optional[str]) -> str:
    if cookie_header:
        return cookie_header
    try:
        session = external_apis.login()
    except external_apis.ExternalApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    return session["cookie_header"]


def _create_prof_claims_for_ids(attachment_ids: List[int], cookie_header: str,
                                run_async: bool) -> List[Dict[str, object]]:
    results = []
    for attachment_id in attachment_ids:
        try:
            payload = external_apis.queue_claim_creation(
                cookie_header=cookie_header,
                attachment_id=attachment_id,
                run_async=run_async,
            )
            db.record_claim_creation_response(attachment_id, payload)
            results.append({
                "attachment_id": attachment_id,
                "run_async": run_async,
                "result": payload,
            })
        except external_apis.ExternalApiError as exc:
            results.append({
                "attachment_id": attachment_id,
                "run_async": run_async,
                "error": str(exc),
            })
    return results


# curl -s -X POST http://localhost:8100/external/claims/create-prof-claim-batch \
#   -H 'content-type: application/json' \
#   -d '{"attachment_ids": [123, 124, 125], "run_async": true, "limit": 20}' | jq
@app.post("/external/claims/create-prof-claim-batch")
def external_create_prof_claim_batch(req: ExternalClaimBatchRequest) -> List[Dict[str, object]]:
    cookie_header = _login_cookie_header(req.cookie_header)
    attachment_ids = req.attachment_ids
    if req.limit is not None:
        attachment_ids = attachment_ids[:req.limit]
    return _create_prof_claims_for_ids(attachment_ids, cookie_header, req.run_async)


# curl -s -X POST http://localhost:8100/external/claims/create-prof-claim-all \
#   -H 'content-type: application/json' \
#   -d '{"run_async": true, "limit": 20}' | jq
@app.post("/external/claims/create-prof-claim-all")
def external_create_prof_claim_all(
        req: ExternalClaimAllRequest = ExternalClaimAllRequest()) -> List[Dict[str, object]]:
    """Sweep every child attachment still in 'G' status with no claim queued
    yet (claim_creation_response IS NULL) and queue claim creation for each."""
    cookie_header = _login_cookie_header(req.cookie_header)
    attachment_ids = db.list_unclaimed_g_status_child_ids()
    if req.limit is not None:
        attachment_ids = attachment_ids[:req.limit]
    return _create_prof_claims_for_ids(attachment_ids, cookie_header, req.run_async)


# curl -s -X POST http://localhost:8100/chargesheet/archive-processed \
#   -H 'content-type: application/json' \
#   -d '{}' | jq
# curl -s -X POST http://localhost:8100/chargesheet/archive-processed \
#   -H 'content-type: application/json' \
#   -d '{"execute": true, "limit": 50}' | jq
@app.post("/chargesheet/archive-processed")
def archive_processed_documents(
        req: ArchiveProcessedRequest = ArchiveProcessedRequest()) -> Dict[str, object]:
    """Archive top-level, processed attachments that aren't already archived:
    rows with parent_attachment_id IS NULL, processed=true, and clm_att_path
    not already under Archive/. Moves each blob and sets status='C', the same
    way _archive_blob_path is used elsewhere. No claim is involved here.

    Defaults to a dry run (execute=false) that only reports what would
    happen; pass execute=true to actually archive."""
    rows = db.list_unarchived_processed_parents()
    candidates = [row for row in rows if row.get("clm_att_path")]
    skipped_no_path = len(rows) - len(candidates)

    limited = candidates[:req.limit] if req.limit is not None else candidates
    planned = [
        {"attachment_id": row["id"], "status": row.get("status"),
         "blob_path": row["clm_att_path"]}
        for row in limited
    ]

    if not req.execute:
        return {
            "dry_run": True,
            "candidate_count": len(candidates),
            "skipped_no_path": skipped_no_path,
            "planned": planned,
        }

    results = []
    for row in limited:
        blob_path = row["clm_att_path"]
        try:
            archive_result = _archive_blob_path(
                blob_path,
                construct_archive_folder_path(blob_path),
                status="C",
                processed=True,
            )
            results.append({
                "attachment_id": row["id"],
                "status": "archived",
                "old_blob_path": blob_path,
                "new_blob_path": archive_result["new_blob_path"],
            })
        except Exception as exc:
            results.append({
                "attachment_id": row["id"],
                "status": "error",
                "blob_path": blob_path,
                "error": str(exc),
            })

    return {
        "dry_run": False,
        "candidate_count": len(candidates),
        "skipped_no_path": skipped_no_path,
        "results": results,
    }
