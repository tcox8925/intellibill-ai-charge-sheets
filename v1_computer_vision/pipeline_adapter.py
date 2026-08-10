"""
pipeline_adapter.py — format-compatibility wrapper, NOT a reimplementation.

Every actual piece of detection/extraction/rendering logic here is delegated
unchanged to this package's own modules (render.py, template_registry.py,
locked_template.py, extract.py, dates.py, run.py's header cache helper).
This file only reshapes calls/results so the locked-template computer-vision
pipeline can be called with the exact same function signature and return
shape as the main repo's run.process_pdf(pdf_path, client, registry, *,
pages_dir, want, auto_build, on_page) -> (results, metrics) — a drop-in swap
for callers (api.py, image_ocr.py) with no changes needed on their side.

Nothing here changes core logic in either pipeline.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # so this package's own dates/render/settings/etc. resolve
if _PARENT not in sys.path:
    sys.path.append(_PARENT)  # lower priority; only for extraction_flags (unique name)

try:
    from extraction_flags import (FLAG_NOT_CHARGESHEET, FLAG_SKIPPED_NO_EXTRACTION,
                                  FLAG_BLANK_HEADER)
except ImportError:
    # Standalone fallback if this package is ever used outside the main repo.
    FLAG_NOT_CHARGESHEET = "not_chargesheet"
    FLAG_SKIPPED_NO_EXTRACTION = "skipped_no_extraction"
    FLAG_BLANK_HEADER = "blank_header"


def _exec_own_module(module_filename: str):
    path = os.path.join(_HERE, f"{module_filename}.py")
    spec = importlib.util.spec_from_file_location(module_filename, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_filename] = module  # visible to the module's own sibling imports
    spec.loader.exec_module(module)
    return module


def _load_own_modules_isolated(*module_filenames: str) -> tuple:
    """Load this package's own {module_filenames}.py files, even though the
    main repo has same-named extract.py/run.py modules that may already be
    cached in sys.modules under those bare names — which would otherwise
    silently win and break this package's internal `from extract import ...`
    -style imports. Hides any conflicting cache entries for the whole load
    (including modules pulled in as a side effect, e.g. run.py's own `from
    extract import ...`), then restores them, so the main repo's run/extract
    stay untouched for every other importer in the process."""
    saved = {name: sys.modules.pop(name, None) for name in module_filenames}
    try:
        return tuple(_exec_own_module(name) for name in module_filenames)
    finally:
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod
            else:
                sys.modules.pop(name, None)


from dates import normalize_header_dates
from model_client import make_client as _make_client
from render import render_pdf
from settings import get_settings
from template_registry import load_manifest_and_catalog, locked_template

# extract.py and run.py collide by name with the main repo's own extract.py/
# run.py, so they're loaded in isolation rather than via a plain `import`.
_v1_extract, _v1_run = _load_own_modules_isolated("extract", "run")
extract_header_notes = _v1_extract.extract_header_notes


def _write_page_png(pages_dir: str, page_number: int, png_bytes: bytes) -> str:
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"page-{page_number:02d}.png")
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def _blank_header(header: dict) -> bool:
    name = (header.get("name") or "").strip()
    dob = (header.get("dob") or "").strip()
    return not (name or dob)


def _mismatch_result(page_number: int, alignment, template) -> dict:
    return {
        "page": page_number,
        "template_ok": False,
        "header": {},
        "circled_procedures": [],
        "circled_diagnoses": [],
        "possible_marks": [],
        "notes": [],
        # extraction_flags-compatible trio (so db.persist_page_v2's
        # ERRORED_CHILD_FLAGS check marks this child attachment 'E'), plus
        # the locked-template-specific flag kept for traceability.
        "flags": [FLAG_NOT_CHARGESHEET, FLAG_SKIPPED_NO_EXTRACTION, FLAG_BLANK_HEADER,
                  "locked_template_mismatch"],
        "circle_detection": {
            "mode": "locked_coordinates_fail_closed",
            "template_id": template.catalog["template_id"],
            "selected_count": 0,
        },
        "template_match": {
            "state": FLAG_NOT_CHARGESHEET,  # old-format state name callers filter on
            "score": round(alignment.match_score, 4),
            "template_id": template.catalog["template_id"],
        },
        "recognition": {
            "is_chargesheet": False,
            "confidence": round(alignment.match_score, 4),
            "method": "locked_template_registration",
        },
        "orientation": {
            "applied_rotation_deg": alignment.rotation_ccw,
            "detected": True,
            "method": "locked_template_registration",
        },
    }


def _matched_result(page_number: int, page_sha256: str, alignment, template,
                    manifest: dict, header_notes: dict, header_source: str) -> dict:
    header = header_notes.get("header") or {}
    _, _, date_flags = normalize_header_dates(header)
    procedures, diagnoses, geometry = template.detect(alignment)

    flags = list(header_notes.get("flags") or []) + date_flags + ["locked_coordinate_selection"]
    if geometry.get("color_selected_count"):
        flags.append("color_circle_geometry")
    if geometry.get("residual_selected_count"):
        flags.append("black_circle_geometry")
    if not geometry.get("residual_geometry_enabled", True):
        flags.append("black_circle_detection_suppressed_low_alignment")
    if header_source == "cache":
        flags.append("header_notes_reused_from_cache")
    if _blank_header(header):
        flags.append(FLAG_BLANK_HEADER)
    flags = list(dict.fromkeys(str(x) for x in flags if str(x).strip()))

    return {
        "page": page_number,
        "page_sha256": page_sha256,
        "template_ok": True,
        "header": header,
        "circled_procedures": procedures,
        "circled_diagnoses": diagnoses,
        "possible_marks": [],
        "notes": header_notes.get("notes") or [],
        "flags": flags,
        "header_notes_source": header_source,
        "circle_detection": geometry,
        "template_match": {
            "state": "known",  # old-format state name so build_metrics-style
                               # consumers count this page as extracted
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


def orientation_info(results: list) -> dict:
    dist = {}
    for r in results:
        deg = (r.get("orientation") or {}).get("applied_rotation_deg")
        if deg is not None:
            dist[str(deg)] = dist.get(str(deg), 0) + 1
    return {
        "detected": True,
        "method": "locked_template_registration",
        "result": "upright",
        "applied_rotation_deg_distribution": dist,
    }


def _flag_counts(results: list) -> dict:
    counts: dict = {}
    for r in results:
        for flag in r.get("flags", []) or []:
            counts[flag] = counts.get(flag, 0) + 1
    return counts


def build_metrics(pdf_path: str, results: list) -> dict:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    def _state(r):
        return r.get("template_match", {}).get("state")

    not_cs = sum(1 for r in results if _state(r) == FLAG_NOT_CHARGESHEET)
    extracted = sum(1 for r in results if _state(r) == "known" and "_error" not in r)
    return {
        "timestamp": ts,
        "source_pdf": os.path.basename(pdf_path),
        "orientation": orientation_info(results),
        "pages_processed": len(results),
        "pages_extracted": extracted,
        "pages_not_chargesheet": not_cs,
        "pages_flagged": sum(1 for r in results if r.get("flags")),
        "pages_autobuilt": 0,  # no catalog auto-build concept in the locked-template pipeline
        "total_procedures": sum(len(r.get("circled_procedures", [])) for r in results),
        "total_diagnoses": sum(len(r.get("circled_diagnoses", [])) for r in results),
        "total_notes": sum(len(r.get("notes", [])) for r in results),
        "flag_counts": _flag_counts(results),
        "pipeline": "v1_computer_vision",
    }


def process_pdf(pdf_path: str, client=None, registry=None, *, pages_dir: str = "pages",
                want=None, auto_build: bool = True, on_page=None):
    """Drop-in, format-compatible replacement for run.process_pdf().

    Same call shape as the LLM pipeline:
        process_pdf(pdf_path, client, registry, *, pages_dir, want, auto_build,
                    on_page) -> (results, metrics)
    with on_page(page_number, page_image_path, result) invoked once per page,
    exactly like the existing pipeline — this is what lets api.py's on_page
    hook (per-page blob upload + Postgres persist) work unchanged.

    `registry` and `auto_build` are accepted only for signature compatibility
    and are unused: this pipeline has exactly one locked template, not a
    catalog registry, and never auto-builds new templates. `client` is
    accepted for injectability; if omitted, one is built via this package's
    own model_client.make_client(). All detection/extraction/rendering logic
    is delegated to render.py, locked_template.py, extract.py, and dates.py,
    unchanged.
    """
    s = get_settings()
    manifest, _, _ = load_manifest_and_catalog()
    template = locked_template()

    pdf_bytes = Path(pdf_path).read_bytes()
    pages = render_pdf(pdf_bytes, s.render_dpi, wanted=set(want) if want else None)

    need_header_notes = s.enable_header_notes
    active_client = client if client is not None else (_make_client() if need_header_notes else None)

    results = []
    for rp in pages:
        page_image_path = _write_page_png(pages_dir, rp.page_number, rp.png_bytes)

        bgr = cv2.imdecode(np.frombuffer(rp.png_bytes, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError(f"Rendered page {rp.page_number} could not be decoded")

        alignment = template.align(bgr)

        if not template.is_match(alignment):
            result = _mismatch_result(rp.page_number, alignment, template)
        else:
            if not need_header_notes:
                header_notes = {"header": {}, "notes": [], "flags": ["header_notes_disabled"]}
                header_source = "disabled"
            else:
                cache_path = _v1_run._header_cache_path(rp.sha256)
                if cache_path.exists():
                    header_notes = json.loads(cache_path.read_text(encoding="utf-8"))
                    header_source = "cache"
                else:
                    rgb = cv2.cvtColor(alignment.color, cv2.COLOR_BGR2RGB)
                    header_notes = extract_header_notes(rgb, active_client)
                    cache_path.write_text(
                        json.dumps(header_notes, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    header_source = "model"
            result = _matched_result(rp.page_number, rp.sha256, alignment, template,
                                     manifest, header_notes, header_source)

        results.append(result)
        if on_page:
            on_page(rp.page_number, page_image_path, result)

    metrics = build_metrics(pdf_path, results)
    return results, metrics
