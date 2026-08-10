"""Clean charge-sheet extraction runner.

Selected CPT/HCPCS/ICD codes are determined only by locked template geometry.
The model is used only for variable header/payment fields and handwritten clinical notes.

Examples:
  python run.py input.pdf --out results.json
  python run.py input.pdf --pages 1,4,5,11 --out spot.json
  python run.py input.pdf --no-ai --out geometry_only.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from dates import normalize_header_dates
from extract import extract_header_notes
from model_client import make_client
from render import render_pdf
from settings import get_settings
from template_registry import load_manifest_and_catalog, locked_template


def _header_cache_path(page_sha: str) -> Path:
    s = get_settings()
    key = hashlib.sha256(
        f"{page_sha}|{s.header_model}|{s.header_extractor_version}".encode("utf-8")
    ).hexdigest()
    p = Path(__file__).resolve().parent / "runtime" / "header_cache" / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _empty_page(page_number: int, score: float, template_id: str, rotation: int | None) -> dict:
    return {
        "page": page_number,
        "template_ok": False,
        "header": {},
        "circled_procedures": [],
        "circled_diagnoses": [],
        "possible_marks": [],
        "notes": [],
        "flags": ["locked_template_mismatch", "skipped_no_extraction"],
        "circle_detection": {
            "mode": "locked_coordinates_fail_closed",
            "template_id": template_id,
            "selected_count": 0,
        },
        "template_match": {
            "state": "locked_mismatch",
            "score": round(score, 4),
            "template_id": template_id,
        },
        "recognition": {
            "is_chargesheet": False,
            "confidence": round(score, 4),
            "method": "locked_template_registration",
        },
        "orientation": {
            "applied_rotation_deg": rotation,
            "detected": rotation is not None,
            "method": "locked_template_registration",
        },
    }


def process_pdf(pdf_path: str, *, wanted: set[int] | None = None, no_ai: bool = False,
                refresh_header_cache: bool = False) -> dict:
    s = get_settings()
    manifest, _, _ = load_manifest_and_catalog()
    template = locked_template()

    pdf = Path(pdf_path)
    pdf_bytes = pdf.read_bytes()
    pages = render_pdf(pdf_bytes, s.render_dpi, wanted=wanted)
    client = None if no_ai or not s.enable_header_notes else make_client()

    results: list[dict] = []
    rotation_dist: dict[str, int] = {}

    for rp in pages:
        bgr = cv2.imdecode(np.frombuffer(rp.png_bytes, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError(f"Rendered page {rp.page_number} could not be decoded")

        alignment = template.align(bgr)
        rotation_dist[str(alignment.rotation_ccw)] = rotation_dist.get(str(alignment.rotation_ccw), 0) + 1

        if not template.is_match(alignment):
            result = _empty_page(
                rp.page_number,
                alignment.match_score,
                template.catalog["template_id"],
                alignment.rotation_ccw,
            )
            results.append(result)
            print(
                f"page {rp.page_number:>3}: [mismatch] proc=0 dx=0 notes=0 "
                f"match={alignment.match_score:.3f}"
            )
            continue

        procedures, diagnoses, geometry = template.detect(alignment)

        if no_ai or not s.enable_header_notes:
            hn = {"header": {}, "notes": [], "flags": ["header_notes_disabled"]}
            header_source = "disabled"
        else:
            cp = _header_cache_path(rp.sha256)
            if cp.exists() and not refresh_header_cache:
                hn = json.loads(cp.read_text(encoding="utf-8"))
                header_source = "cache"
            else:
                rgb = cv2.cvtColor(alignment.color, cv2.COLOR_BGR2RGB)
                hn = extract_header_notes(rgb, client)
                cp.write_text(json.dumps(hn, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                header_source = "model"

        header = hn.get("header") or {}
        _, _, date_flags = normalize_header_dates(header)
        flags = list(hn.get("flags") or []) + date_flags + ["locked_coordinate_selection"]
        if geometry.get("color_selected_count"):
            flags.append("color_circle_geometry")
        if geometry.get("residual_selected_count"):
            flags.append("black_circle_geometry")
        if not geometry.get("residual_geometry_enabled", True):
            flags.append("black_circle_detection_suppressed_low_alignment")
        if header_source == "cache":
            flags.append("header_notes_reused_from_cache")
        flags = list(dict.fromkeys(str(x) for x in flags if str(x).strip()))

        result = {
            "page": rp.page_number,
            "page_sha256": rp.sha256,
            "template_ok": True,
            "header": header,
            "circled_procedures": procedures,
            "circled_diagnoses": diagnoses,
            "possible_marks": [],
            "notes": hn.get("notes") or [],
            "flags": flags,
            "header_notes_source": header_source,
            "circle_detection": geometry,
            "template_match": {
                "state": "locked",
                "score": round(alignment.match_score, 4),
                "catalog": "catalog.json",
                "template_id": template.catalog["template_id"],
                "template_version": int(manifest["version"]),
            },
            "recognition": {
                "is_chargesheet": True,
                "confidence": round(alignment.match_score, 4),
                "method": "locked_template_registration",
            },
            "orientation": {
                "applied_rotation_deg": alignment.rotation_ccw,
                "detected": True,
                "method": "locked_template_registration",
            },
        }
        results.append(result)
        print(
            f"page {rp.page_number:>3}: [locked] proc={len(procedures)} dx={len(diagnoses)} "
            f"notes={len(result['notes'])} match={alignment.match_score:.3f} header={header_source}"
        )

    return {
        "source_pdf": pdf.name,
        "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "page_count": len(results),
        "pipeline": s.pipeline_version,
        "template_id": s.template_id,
        "template_version": s.template_version,
        "orientation": {
            "detected": True,
            "method": "locked_template_registration",
            "result": "upright",
            "applied_rotation_deg_distribution": rotation_dist,
        },
        "pages": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--pages", default="", help="comma-separated 1-based page numbers")
    ap.add_argument("--no-ai", action="store_true", help="geometry only; no header/notes model call")
    ap.add_argument(
        "--refresh-header-cache",
        action="store_true",
        help="re-run header/notes model instead of reusing cached transcription",
    )
    args = ap.parse_args()

    wanted = {int(x) for x in args.pages.split(",") if x.strip()} if args.pages else None
    payload = process_pdf(
        args.pdf,
        wanted=wanted,
        no_ai=args.no_ai,
        refresh_header_cache=args.refresh_header_cache,
    )
    Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out} ({payload['page_count']} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
