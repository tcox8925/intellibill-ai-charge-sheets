"""Helpers for running charge-sheet OCR on a single image file.

This wraps the existing single-page extraction building blocks so callers can
pass PNG/JPEG/TIFF/WebP/etc., normalize to PNG when needed, and get back a
result structure that mirrors the PDF pipeline for one page.
"""

import json
import os
import tempfile
from typing import Optional, Set

from PIL import Image

from extract import extract_page, identify_page, load_page_b64
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


def process_image(image_path: str, client=None, registry=None,
                  auto_build: bool = True) -> dict:
    """Process a single image and return a one-page OCR result payload."""
    client = client or run.make_client()
    registry = registry or run.load_registry()

    with tempfile.TemporaryDirectory() as tmp:
        png_path = normalize_image_to_png(image_path, tmp)

        seen_labels, seen_codes = identify_page(png_path, client)
        cat_path, catalog, score = run.pick_catalog(seen_labels, seen_codes, registry)
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
        header = result.get("header", {}) or {}
        if not any(((header.get("name") or "").strip(), (header.get("dob") or "").strip())):
            result.setdefault("flags", []).append("blank_header")
        result["page"] = 1

    payload = {
        "source_file": os.path.basename(image_path),
        "page_count": 1,
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