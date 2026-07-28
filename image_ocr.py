"""Helpers for running charge-sheet OCR on a single image file.

This wraps the existing single-page extraction building blocks so callers can
pass PNG/JPEG/TIFF/WebP/etc., normalize to PNG when needed, and get back a
result structure that mirrors the PDF pipeline for one page.
"""

import json
import os
import tempfile
from typing import Optional

from PIL import Image

from extract import extract_page, identify_page, load_page_b64, detect_orientation
from fingerprint import STRONG, _norm
import run


def normalize_image_to_png(image_path: str, out_dir: Optional[str] = None) -> str:
    """Convert any Pillow-readable image to a PNG and return the PNG path."""
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="chargesheet-image-")
    os.makedirs(out_dir, exist_ok=True)

    stem = os.path.splitext(os.path.basename(image_path))[0] or "image"
    out_path = os.path.join(out_dir, f"{stem}.png")

    with Image.open(image_path) as image:
        image.convert("RGB").save(out_path, format="PNG")

    return out_path


def prepare_image_for_ocr(image_path: str, client, out_dir: Optional[str] = None) -> tuple[str, int]:
    """Normalize an image to PNG, detect its orientation, and write back an
    upright PNG for downstream OCR and upload."""
    png_path = normalize_image_to_png(image_path, out_dir)
    rotation = detect_orientation(png_path, client)
    if rotation:
        with Image.open(png_path) as image:
            image.rotate(rotation, expand=True).save(png_path, format="PNG")
    return png_path, rotation


def process_image(image_path: str, client=None, registry=None,
                  auto_build: bool = True) -> dict:
    """Process a single image and return a one-page OCR result payload."""
    client = client or run.make_client()
    registry = registry or run.load_registry()

    with tempfile.TemporaryDirectory() as tmp:
        png_path, rotation = prepare_image_for_ocr(image_path, client, tmp)

        seen_labels, seen_codes, is_cs, cs_conf = identify_page(png_path, client)
        cat_path, catalog, score = run.pick_catalog(seen_labels, seen_codes, registry)
        recognized = run.looks_like_chargesheet(is_cs, cs_conf, seen_labels, seen_codes) \
            or score >= STRONG

        if not recognized:
            result = {
                "page": 1,
                "template_ok": False,
                "orientation": {
                    "applied_rotation_deg": rotation,
                    "detected": True,
                    "method": "haiku",
                    "raw_detected_deg": rotation,
                },
                "header": {},
                "circled_procedures": [],
                "circled_diagnoses": [],
                "possible_marks": [],
                "notes": [],
                "recognition": {
                    "is_chargesheet": False,
                    "confidence": cs_conf,
                    "seen_sections": seen_labels,
                    "seen_codes_sample": (seen_codes or [])[:8],
                },
                "flags": ["not_chargesheet", "skipped_no_extraction"],
                "template_match": {"state": "not_chargesheet", "score": round(score, 2)},
            }
            return {
                "source_file": os.path.basename(image_path),
                "page_count": 1,
                "orientation": run.orientation_info([result]),
                "pages": [result],
                "metrics": run.build_metrics(image_path, [result]),
            }

        template_state = "known" if score >= STRONG else "miss"

        if template_state == "miss" and not seen_labels and not seen_codes and registry:
            cat_path, catalog = registry[0]
            template_state = "known"

        if template_state == "miss":
            if not auto_build:
                result = {
                    "page": 1,
                    "flags": ["template_miss_skipped"],
                    "template_match": {"state": "miss", "score": round(score, 2)},
                }
                return {
                    "source_file": os.path.basename(image_path),
                    "page_count": 1,
                    "pages": [result],
                    "metrics": run.build_metrics(image_path, [result]),
                }

            key = "|".join(sorted(_norm(label) for label in seen_labels)) or "unknown"
            template_id = "autobuilt_" + run.hashlib.md5(key.encode()).hexdigest()[:8]
            cat_path = f"catalog_{template_id}.json"
            if not os.path.exists(cat_path):
                run.build_catalog(png_path, client, page=1, out=cat_path, template_id=template_id)
            with open(cat_path) as file_obj:
                catalog = json.load(file_obj)
            registry.append((cat_path, catalog))
            template_state = "autobuilt"

        result = extract_page(load_page_b64(png_path), catalog, client)
        result["template_match"] = {
            "state": template_state,
            "score": round(score, 2),
            "catalog": os.path.basename(cat_path),
            "template_id": catalog.get("template_id"),
        }
        result["recognition"] = {"is_chargesheet": True, "confidence": cs_conf}
        result["orientation"] = {
            "applied_rotation_deg": rotation,
            "detected": True,
            "method": "haiku",
            "raw_detected_deg": rotation,
        }
        if result.get("template_ok") is False:
            result.setdefault("flags", []).append("template_mismatch")
        header = result.get("header", {}) or {}
        for flag in run.check_dates(header):
            result.setdefault("flags", []).append(flag)
        if not any(((header.get("name") or "").strip(), (header.get("dob") or "").strip())):
            result.setdefault("flags", []).append("blank_header")
        result["page"] = 1

    payload = {
        "source_file": os.path.basename(image_path),
        "page_count": 1,
        "orientation": run.orientation_info([result]),
        "pages": [result],
        "metrics": run.build_metrics(image_path, [result]),
    }

    return payload


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="path to a page image to OCR")
    parser.add_argument("--out", default=None,
                        help="optional output JSON path; defaults next to the image")
    parser.add_argument("--no-auto-build", action="store_true",
                        help="skip auto-building a catalog for unknown forms")
    args = parser.parse_args()

    output_path = args.out
    if output_path is None:
        folder = os.path.dirname(args.image) or "."
        stem = os.path.splitext(os.path.basename(args.image))[0]
        output_path = os.path.join(folder, f"results-{stem}.json")

    payload = process_image(args.image, auto_build=not args.no_auto_build)
    with open(output_path, "w") as file_obj:
        json.dump(payload, file_obj, indent=2)
    print(f"wrote {output_path}")


if __name__ == "__main__":
    main()