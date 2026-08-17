from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    png_bytes: bytes
    sha256: str


def render_pdf(pdf_bytes: bytes, dpi: int = 200, wanted: set[int] | None = None) -> list[RenderedPage]:
    """Render with Poppler/pdftoppm.

    IMPORTANT: the locked reference/template was calibrated against this renderer.
    Do not silently switch render engines; a renderer change is a template-version
    change and must be regression-tested/recalibrated.
    """
    with tempfile.TemporaryDirectory(prefix="chargesheet-render-") as td:
        root = Path(td)
        pdf = root / "source.pdf"
        pdf.write_bytes(pdf_bytes)

        found: list[tuple[int, Path]] = []
        if wanted:
            for page_number in sorted(wanted):
                prefix = root / f"selected-{page_number:04d}"
                subprocess.run(
                    ["pdftoppm", "-f", str(page_number), "-l", str(page_number),
                     "-singlefile", "-png", "-r", str(dpi), str(pdf), str(prefix)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                path = Path(str(prefix) + ".png")
                if path.exists():
                    found.append((page_number, path))
        else:
            prefix = root / "page"
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(prefix)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            for path in root.glob("page-*.png"):
                m = re.search(r"page-(\d+)\.png$", path.name)
                if m:
                    found.append((int(m.group(1)), path))
            found.sort()

        if not found:
            raise RuntimeError("pdftoppm produced no page images")
        out = []
        for page_number, path in found:
            data = path.read_bytes()
            out.append(RenderedPage(page_number, data, hashlib.sha256(data).hexdigest()))
        return out
