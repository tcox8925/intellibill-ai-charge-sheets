"""
storage.py — Azure Blob helper for the charge-sheet pipeline.

Container layout (container = 834labs-sftp on ibrcmdataprd001):

    {practice}/{file}.pdf                        <- SFTP drop (source)
    {practice}/pages/{pdf_stem}/page-01.png      <- rendered pages (we create)
    {practice}/pages/{pdf_stem}/page-02.png
    ...

The practice is GLOBAL (one practice per deployment) — see PRACTICE below.
Callers don't pass it around; every path helper defaults to it.

AUTH — preferred path is managed identity / DefaultAzureCredential (no secret
in source). The connection-string block below is a fallback; its AccountKey is
a LIVE PROD key. It MUST be rotated and moved to Key Vault before this ships.
The code never uses the hardcoded value — it prefers managed identity and only
reads a connection string from the environment.
"""

import io
import os
import re
from typing import Optional
from PIL import Image

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

# =====================================================================
# PRACTICE — GLOBAL CONFIG
# ---------------------------------------------------------------------
# >>> UPDATE HERE to point the pipeline at a different practice. <<<
# Must match the blob folder name in 834labs-sftp EXACTLY, e.g.
#   "PrePost+ Tennessee" | "PrePostPlus Germantown" | "The PreOP Center"
PRACTICE = os.environ.get("AZURE_STORAGE_PRACTICE", "The PreOP Center")
# =====================================================================

STORAGE_ACCOUNT_NAME = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "ibrcmdataprd001")
CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER", "rcm-attachments")
ACCOUNT_URL = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net"


def get_blob_service() -> BlobServiceClient:
    """Prefer managed identity (no secret); fall back to a connection string
    ONLY if one is present in the environment. Never uses the hardcoded key."""
    env_cs = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if env_cs:
        return BlobServiceClient.from_connection_string(env_cs)
    return BlobServiceClient(ACCOUNT_URL, credential=DefaultAzureCredential())


def _container():
    return get_blob_service().get_container_client(CONTAINER)


def check_blob_connection() -> dict:
    """Return Azure Blob connectivity status for the configured container."""
    auth_mode = "connection_string" if os.environ.get("AZURE_STORAGE_CONNECTION_STRING") else "default_azure_credential"
    try:
        container = _container()
        props = container.get_container_properties()
        return {
            "connected": True,
            "account_url": ACCOUNT_URL,
            "container": CONTAINER,
            "practice": PRACTICE,
            "auth_mode": auth_mode,
            "last_modified": props.last_modified.isoformat() if getattr(props, "last_modified", None) else None,
        }
    except Exception as exc:
        return {
            "connected": False,
            "account_url": ACCOUNT_URL,
            "container": CONTAINER,
            "practice": PRACTICE,
            "auth_mode": auth_mode,
            "error": str(exc),
        }


def list_folders() -> list[str]:
    """Top-level 'folders' in the container (delimiter walk). This is every
    folder, not just practices — the container also holds e.g. EOB. The
    pipeline itself only runs the single global PRACTICE."""
    cc = _container()
    folders = []
    for prefix in cc.walk_blobs(name_starts_with="", delimiter="/"):
        name = getattr(prefix, "name", "")
        if name.endswith("/"):
            folders.append(name[:-1])
    return sorted(folders)


def list_files(prefix: str) -> list[str]:
    """List blob files directly under a prefix, excluding nested children."""
    cc = _container()
    normalized = prefix.rstrip("/") + "/"
    files = []
    for blob in cc.list_blobs(name_starts_with=normalized):
        rel = blob.name[len(normalized):]
        if not rel or "/" in rel:
            continue
        files.append(blob.name)
    return sorted(files)


def list_claim_files(entity_folder: str,
                     exchange_folder: str = "Exchange",
                     documents_folder: str = "Documents",
                     claim_files_folder: str = "Claim files") -> list[str]:
    """List files under <entity>/Exchange/Documents/Claim files/."""
    prefix = "/".join([
        entity_folder.strip("/"),
        exchange_folder.strip("/"),
        documents_folder.strip("/"),
        claim_files_folder.strip("/"),
    ])
    return list_files(prefix)


def is_entity_group_folder(folder_name: str) -> bool:
    return bool(re.fullmatch(r"\d+-\d+", folder_name or ""))


def find_source_pdf(filename: Optional[str] = None,
                    practice: Optional[str] = None) -> Optional[str]:
    """Return the PDF blob path directly under {practice}/. If filename is
    given, match it exactly; otherwise pick the newest .pdf at the practice
    root (ignores anything under {practice}/pages/). Defaults to global PRACTICE."""
    practice = practice or PRACTICE
    cc = _container()
    prefix = f"{practice}/"
    candidates = []
    for b in cc.list_blobs(name_starts_with=prefix):
        rel = b.name[len(prefix):]
        if "/" in rel:                       # nested (e.g. pages/...) — skip
            continue
        if not rel.lower().endswith(".pdf"):
            continue
        if filename and rel != filename:
            continue
        candidates.append((b.last_modified, b.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)            # newest first
    return candidates[0][1]


def download_blob(blob_path: str) -> bytes:
    return _container().download_blob(blob_path).readall()


def download_pdf(blob_path: str) -> bytes:
    return download_blob(blob_path)


def is_pdf_path(blob_path: str) -> bool:
    return blob_path.lower().endswith(".pdf")


def is_image_path(blob_path: str) -> bool:
    return os.path.splitext(blob_path)[1].lower() in {
        ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"
    }


def pages_prefix(pdf_stem: str, practice: Optional[str] = None) -> str:
    return f"{practice or PRACTICE}/pages/{pdf_stem}/"


def page_blob_path(pdf_stem: str, page_number: int,
                   practice: Optional[str] = None) -> str:
    return f"{pages_prefix(pdf_stem, practice)}page-{page_number:02d}.png"


def upload_page(pdf_stem: str, page_number: int,
                image: "Image.Image | bytes",
                practice: Optional[str] = None) -> str:
    """Upload one rendered page PNG; return its blob path (store this in
    wpo.chargesheet_pages.page_blob_path)."""
    if isinstance(image, Image.Image):
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        data = buf.getvalue()
    else:
        data = image
    path = page_blob_path(pdf_stem, page_number, practice)
    _container().upload_blob(name=path, data=data, overwrite=True)
    return path
