"""
api.py — FastAPI service for the charge-sheet pipeline (kept SEPARATE from the
run.py CLI). It wires the shared extraction core (run.process_pdf) to blob
storage (storage.py) and Postgres (db.py).

Endpoints
    GET  /health                                      simple service health check
    POST /chargesheet/extract                         upload one PDF and return extraction JSON
    GET  /chargesheet/folders                         list container folders
    POST /chargesheet/ingest                          {filename?} -> document_id
    GET  /chargesheet/documents/{document_id}         status + metrics
    GET  /chargesheet/documents/{document_id}/pages   per-page results + extraction_ids
    POST /chargesheet/feedback                         the two review signals
    GET  /chargesheet/documents/{document_id}/feedback list feedback

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
from typing import Dict, List
from typing import Optional, Union

from fastapi import FastAPI, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, model_validator

import auth
import storage
import db
import image_ocr
import run

app = FastAPI(title="834 Charge-sheet OCR", version="1.0")

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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


class IngestAllResult(BaseModel):
    queued: List[Dict[str, object]]
    skipped: List["IngestSkipResult"]
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


# ---------- endpoints -------------------------------------------------------

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


@app.post("/chargesheet/extract")
async def extract_pdf(file: UploadFile = File(...), pages: Optional[str] = None):
    filename = file.filename or "upload.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "uploaded file must be a PDF")

    try:
        want = {int(x) for x in pages.split(",") if x.strip()} if pages else None
    except ValueError:
        raise HTTPException(400, "pages must be a comma-separated 1-based page list")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "uploaded file is empty")

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, filename)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        results, metrics = run.process_pdf(
            pdf_path,
            _client(),
            run.load_registry(),
            pages_dir=os.path.join(tmp, "pages"),
            want=want,
        )

    return {
        "source_pdf": filename,
        "page_count": len(results),
        "pages": results,
        "metrics": metrics,
    }

@app.get("/chargesheet/folders")
def folders():
    return {"container": storage.CONTAINER, "folders": storage.list_folders()}


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


@app.post("/chargesheet/ingest-all")
def ingest_all(bg: BackgroundTasks) -> IngestAllResult:
    folder_payload = folders()
    queued = []
    skipped = []

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
            if is_processed(blob_path):
                logger.info("Skipping already processed claim file: %s", blob_path)
                skipped.append(_build_skip_result(
                    blob_path, "already_processed", folder=folder_name))
                continue

            queue_result = _handle_ingest_blob(blob_path, bg)
            if queue_result is None:
                skipped.append(_build_skip_result(
                    blob_path,
                    "unsupported_file_marked_processed", folder=folder_name))
                continue
            queued.append(queue_result)

    return IngestAllResult(
        queued=queued,
        skipped=skipped,
        excluded_folders=INGEST_ALL_EXCLUDE,
    )


def is_processed(blob_path: str) -> bool:
    return db.is_attachment_processed(blob_path)


def mark_completed(blob_path: str, document_id: int):
    updated = db.mark_attachment_processed(blob_path, True)
    return {"blob_path": blob_path, "document_id": document_id, "completed": updated}


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
    mark_completed(blob_path, None)
    logger.info("Auto-marked unsupported claim file as processed: %s", blob_path)


def _handle_ingest_blob(blob_path: str, bg: BackgroundTasks) -> Optional[dict]:
    if storage.is_pdf_path(blob_path) or storage.is_image_path(blob_path):
        return _queue_blob_ingest(blob_path, bg)

    _mark_unsupported_processed(blob_path)
    return None


def _blob_folder(blob_path: str) -> str:
    return blob_path.split("/", 1)[0]


def _queue_blob_ingest(blob_path: str, bg: BackgroundTasks) -> dict:
    data = storage.download_blob(blob_path)
    stem = os.path.splitext(os.path.basename(blob_path))[0]
    folder = _blob_folder(blob_path)
    attachment = db.get_attachment_by_path(blob_path)
    if not attachment:
        raise HTTPException(404, f"attachment not found for blob path: {blob_path}")
    attachment_id = attachment["id"]

    worker = _process if storage.is_pdf_path(blob_path) else _process_image_blob
    source_type = "pdf" if storage.is_pdf_path(blob_path) else "image"
    logger.info("Queueing %s claim file for processing: %s", source_type, blob_path)
    bg.add_task(worker, attachment_id, stem, blob_path, data)
    return {
        "document_id": attachment_id,
        "status": "queued",
        "folder": folder,
        "source_blob_path": f"{storage.CONTAINER}/{blob_path}",
        "pages_blob_prefix": f"{storage.CONTAINER}/{storage.pages_prefix(blob_path)}",
        "source_type": source_type,
    }


def _process(attachment_id: int, stem: str, blob_path: str, pdf_bytes: bytes):
    """Background worker: split -> per-page extract -> upload page -> persist."""
    from PIL import Image
    registry = _registry()
    folder = _blob_folder(blob_path)
    document_id = attachment_id
    try:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "src.pdf")
            with open(pdf_path, "wb") as f:
                f.write(pdf_bytes)

            def on_page(page_no, page_image_path, result):
                # upload the rendered page, then persist page+children with paths
                uploaded_blob_path = storage.upload_page(
                    blob_path, page_no, Image.open(page_image_path))
                result["template_match"] = result.get("template_match", {})
                db.persist_page(document_id, result,
                                page_blob_path=f"{storage.CONTAINER}/{uploaded_blob_path}")

            results, metrics = run.process_pdf(
                pdf_path, _client(), registry,
                pages_dir=os.path.join(tmp, "pages"), on_page=on_page)

        tid = next((r.get("template_match", {}).get("template_id")
                    for r in results if r.get("template_match", {}).get("template_id")), None)
        db.set_document_status(document_id, "done", page_count=len(results),
                               template_id=tid, run_metrics=metrics)
        mark_completed(blob_path, document_id)
    except Exception as e:
        db.set_document_status(document_id, "failed", error=str(e))
        raise


def _process_image_blob(attachment_id: int, stem: str, blob_path: str,
                        image_bytes: bytes):
    """Background worker for single image blobs."""
    from PIL import Image

    registry = _registry()
    folder = _blob_folder(blob_path)
    document_id = attachment_id
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, os.path.basename(blob_path))
            with open(image_path, "wb") as file_obj:
                file_obj.write(image_bytes)

            payload = image_ocr.process_image(image_path, _client(), registry)
            normalized_path = image_ocr.normalize_image_to_png(image_path, tmp)
            uploaded_blob_path = storage.upload_page(
                blob_path, 1, Image.open(normalized_path))

        result = payload["pages"][0]
        db.persist_page(document_id, result,
                        page_blob_path=f"{storage.CONTAINER}/{uploaded_blob_path}")
        tid = result.get("template_match", {}).get("template_id")
        db.set_document_status(document_id, "done", page_count=1,
                               template_id=tid, run_metrics=payload["metrics"])
        mark_completed(blob_path, document_id)
    except Exception as e:
        db.set_document_status(document_id, "failed", error=str(e))
        raise


@app.get("/chargesheet/documents/{document_id}")
def document(document_id: int):
    doc = db.get_document(document_id)
    if not doc:
        raise HTTPException(404, "document not found")
    return doc


@app.get("/chargesheet/documents/{document_id}/pages")
def document_pages(document_id: int):
    if not db.get_document(document_id):
        raise HTTPException(404, "document not found")
    return {"document_id": document_id, "pages": db.get_page_results(document_id)}


@app.post("/chargesheet/feedback")
def feedback(req: FeedbackRequest):
    fid = db.record_feedback(
        document_id=req.document_id, page_number=req.page_number,
        feedback_type=req.feedback_type, target=req.target,
        extraction_id=req.extraction_id, code=req.code,
        correct_code=req.correct_code, correct_value=req.correct_value,
        section=req.section, mark=req.mark, reviewer=req.reviewer, note=req.note)
    return {"feedback_id": fid, "feedback_type": req.feedback_type}


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
