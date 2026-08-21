"""PDF page rendering via Poppler/pdftoppm.

IMPORTANT: the locked reference was calibrated against this renderer at the DPI
declared in manifest.json. A renderer or DPI change is a template-version change
and must be recalibrated, not silently accepted.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    png_bytes: bytes
    sha256: str


def pdftoppm_path() -> str:
    exe = shutil.which("pdftoppm")
    if exe:
        return exe
    raise RuntimeError(
        "pdftoppm not found on PATH. Install Poppler:\n"
        "  Windows : download poppler-windows, add its bin\\ to PATH\n"
        "  macOS   : brew install poppler\n"
        "  Debian  : sudo apt-get install poppler-utils"
    )


def render_pdf(pdf_bytes: bytes, dpi: int = 200,
               wanted: set[int] | None = None) -> list[RenderedPage]:
    exe = pdftoppm_path()
    with tempfile.TemporaryDirectory(prefix="chargesheet-render-") as td:
        root = Path(td)
        pdf = root / "source.pdf"
        pdf.write_bytes(pdf_bytes)

        found: list[tuple[int, Path]] = []
        if wanted:
            for n in sorted(wanted):
                prefix = root / f"selected-{n:04d}"
                subprocess.run(
                    [exe, "-f", str(n), "-l", str(n), "-singlefile", "-png",
                     "-r", str(dpi), str(pdf), str(prefix)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                p = Path(str(prefix) + ".png")
                if p.exists():
                    found.append((n, p))
        else:
            prefix = root / "page"
            subprocess.run(
                [exe, "-png", "-r", str(dpi), str(pdf), str(prefix)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for p in root.glob("page-*.png"):
                m = re.search(r"page-(\d+)\.png$", p.name)
                if m:
                    found.append((int(m.group(1)), p))
            found.sort()

        if not found:
            raise RuntimeError("pdftoppm produced no page images")
        out = []
        for n, p in found:
            data = p.read_bytes()
            out.append(RenderedPage(n, data, hashlib.sha256(data).hexdigest()))
        return out
