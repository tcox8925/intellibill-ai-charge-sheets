"""Split a multi-page PDF into per-patient PDFs using extraction results.

Usage:
    python split_by_patient.py <source_pdf> <results_json> [--out-dir out]

The helper groups pages by the extracted header.name field from results JSON.
Pages with the same normalized patient name are written into one output PDF.
Pages without a usable patient name are written as one-page PDFs under an
"unknown" filename.
"""

import argparse
import json
import os
import re
from collections import OrderedDict
from typing import List, Optional, Tuple

from pypdf import PdfReader, PdfWriter


def normalize_patient_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def sanitize_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", text).strip().replace(" ", "_")
    return cleaned or "unknown"


def group_pages_by_patient(results: dict) -> List[Tuple[str, List[int]]]:
    groups = OrderedDict()
    unknown_index = 1

    for page in results.get("pages", []):
        page_number = page.get("page")
        if not isinstance(page_number, int):
            continue

        raw_name = ((page.get("header") or {}).get("name") or "").strip()
        normalized = normalize_patient_name(raw_name)

        if normalized:
            if normalized not in groups:
                groups[normalized] = {"label": raw_name, "pages": []}
            groups[normalized]["pages"].append(page_number)
            continue

        label = f"unknown_page_{unknown_index:02d}"
        groups[f"__unknown_{unknown_index:02d}"] = {"label": label, "pages": [page_number]}
        unknown_index += 1

    return [(item["label"], item["pages"]) for item in groups.values()]


def split_pdf_by_patient(source_pdf: str, results_json: str,
                         out_dir: Optional[str] = None) -> List[str]:
    results = json.load(open(results_json))
    reader = PdfReader(source_pdf)

    if out_dir is None:
        source_stem = os.path.splitext(os.path.basename(source_pdf))[0]
        out_dir = os.path.join(os.path.dirname(source_pdf) or ".", f"{source_stem}_by_patient")
    os.makedirs(out_dir, exist_ok=True)

    written = []
    for label, page_numbers in group_pages_by_patient(results):
        writer = PdfWriter()
        for page_number in page_numbers:
            writer.add_page(reader.pages[page_number - 1])

        out_name = f"{sanitize_filename(label)}.pdf"
        out_path = os.path.join(out_dir, out_name)
        with open(out_path, "wb") as f:
            writer.write(f)
        written.append(out_path)

    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source_pdf", help="source multi-page PDF to split")
    ap.add_argument("results_json", help="results JSON containing page -> patient name mapping")
    ap.add_argument("--out-dir", default=None, help="output directory for per-patient PDFs")
    args = ap.parse_args()

    written = split_pdf_by_patient(args.source_pdf, args.results_json, args.out_dir)
    print(f"wrote {len(written)} patient PDFs")
    for path in written:
        print(path)


if __name__ == "__main__":
    main()