"""Shared PDF rasterization helpers.

Uses PyMuPDF so the pipeline stays self-contained and does not depend on
external PDF rasterization binaries in the runtime environment.
"""

import os
from typing import Optional


def render_pdf_pages(pdf_path: str, out_dir: str, dpi: int) -> list[str]:
    import fitz

    os.makedirs(out_dir, exist_ok=True)
    pages = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            out_path = os.path.join(out_dir, f"page-{index:02d}.png")
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pixmap.save(out_path)
            pages.append(out_path)

    return pages


def render_pdf_page(pdf_path: str, out_dir: str, page_number: int,
                    dpi: int) -> str:
    import fitz

    os.makedirs(out_dir, exist_ok=True)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        if page_number < 1 or page_number > len(doc):
            raise ValueError(
                f"page {page_number} out of range for {pdf_path} ({len(doc)} pages)"
            )
        page = doc[page_number - 1]
        out_path = os.path.join(out_dir, f"page-{page_number:02d}.png")
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        pixmap.save(out_path)

    return out_path