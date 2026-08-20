"""
pipeline_adapter.py — format-compatibility wrapper, NOT a reimplementation.

Every actual piece of detection/extraction/rendering/AI-mark-review logic here
is delegated unchanged to this package's own modules (render.py,
template_registry.py, locked_template.py, extract.py, mark_resolver.py,
dates.py, run.py's cache-path/manual-review helpers). This file only reshapes
calls/results so the v2.2.1 hybrid pipeline can be called with the exact same
function signature and return shape as the main repo's
run.process_pdf(pdf_path, client, registry, *, pages_dir, want, auto_build,
on_page) -> (results, metrics) — a drop-in swap for callers (api.py,
image_ocr.py) with no changes needed on their side.

v2.2.1's own run.py does not expose a pages_dir/on_page hook (it renders and
returns one big dict internally) — this adapter runs the same per-page steps
in the same order (align -> detect -> mark AI review -> manual-review
buckets -> header/notes -> flags -> result dict) so on_page can still be
invoked once per page, exactly like every other pipeline this repo has
integrated. Nothing here changes core logic in any of these pipelines.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("chargesheet.v2_2_1_2_computer_vision")

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
    main repo (and the sibling v1_computer_vision/v2_computer_vision packages)
    have same-named extract.py/run.py modules that may already be cached in
    sys.modules under those bare names — which would otherwise silently win
    and break this package's internal `from extract import ...`-style
    imports. Hides any conflicting cache entries for the whole load (including
    modules pulled in as a side effect, e.g. run.py's own `from mark_resolver
    import ...`), then restores them, so no other importer in the process is
    affected."""
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
from mark_resolver import apply_mark_review, review_possible_marks
from model_client import make_client as _make_client
from render import render_pdf
from settings import get_settings
from template_registry import load_manifest_and_catalog, locked_template

# extract.py and run.py collide by name with the main repo's own extract.py/
# run.py (and with the sibling v1/v2 computer-vision packages' copies), so
# they're loaded in isolation rather than via a plain `import`.
_v221_extract, _v221_run = _load_own_modules_isolated("extract", "run")
extract_header_notes = _v221_extract.extract_header_notes


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
        "manual_review_procedures": [],
        "manual_review_diagnoses": [],
        "possible_marks": [],
        "suppressed_marks": [],
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
                    manifest: dict, header_notes: dict, header_source: str,
                    procedures: list, diagnoses: list, manual_review_procedures: list,
                    manual_review_diagnoses: list, possible_marks: list,
                    suppressed_marks: list, mark_ai_summary: dict, mark_ai_source: str,
                    geometry: dict) -> dict:
    header = header_notes.get("header") or {}
    _, _, date_flags = normalize_header_dates(header)

    surfaced_procedure_codes = []
    for item in list(procedures) + list(manual_review_procedures):
        code = str(item.get("code") or "")
        if code and code not in surfaced_procedure_codes:
            surfaced_procedure_codes.append(code)

    flags = list(header_notes.get("flags") or []) + date_flags + ["locked_coordinate_candidate_source"]
    if geometry.get("color_selected_count"):
        flags.append("color_circle_geometry")
    if geometry.get("residual_selected_count"):
        flags.append("black_circle_geometry")
    if mark_ai_summary.get("promoted_count"):
        flags.append("ai_visual_candidate_promoted")
    if manual_review_procedures or manual_review_diagnoses:
        flags.append("manual_code_review_required")
    if not surfaced_procedure_codes:
        flags.append("procedure_code_missing_invariant")
    if possible_marks:
        flags.append("mark_review_required")
        reasons = {str(m.get("reason", "")) for m in possible_marks}
        if "adjacent_code_overlap" in reasons:
            flags.append("ambiguous_circle_assignment")
        if "partial_circle_low_confidence" in reasons:
            flags.append("partial_circle_low_confidence")
        if "scribbled_region" in reasons:
            flags.append("scribbled_region_review")
    if suppressed_marks:
        flags.append("ai_suppressed_no_deliberate_mark")
    if mark_ai_summary.get("status") == "failed":
        flags.append("mark_ai_review_failed")
    if not geometry.get("residual_geometry_enabled", True):
        flags.append("black_circle_detection_suppressed_low_alignment")
    if header_source == "cache":
        flags.append("header_notes_reused_from_cache")
    if mark_ai_source == "cache":
        flags.append("mark_ai_reused_from_cache")
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
        "manual_review_procedures": manual_review_procedures,
        "manual_review_diagnoses": manual_review_diagnoses,
        "procedure_codes": [
            {**item, "status": "confirmed"} for item in procedures
        ] + manual_review_procedures,
        "procedure_summary": {
            "confirmed_count": len(procedures),
            "manual_review_count": len(manual_review_procedures),
            "surfaced_count": len(surfaced_procedure_codes),
            "invariant_ok": bool(surfaced_procedure_codes),
        },
        "possible_marks": possible_marks,
        "suppressed_marks": suppressed_marks,
        "notes": header_notes.get("notes") or [],
        "flags": flags,
        "header_notes_source": header_source,
        "mark_ai": mark_ai_summary,
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
        "total_manual_review_procedures": sum(len(r.get("manual_review_procedures", [])) for r in results),
        "total_manual_review_diagnoses": sum(len(r.get("manual_review_diagnoses", [])) for r in results),
        "total_notes": sum(len(r.get("notes", [])) for r in results),
        "total_possible_marks": sum(len(r.get("possible_marks", [])) for r in results),
        "flag_counts": _flag_counts(results),
        "pipeline": "v2_2_1_2_computer_vision",
    }


def process_pdf(pdf_path: str, client=None, registry=None, *, pages_dir: str = "pages",
                want=None, auto_build: bool = True, on_page=None,
                no_mark_ai: bool = False):
    """Drop-in, format-compatible replacement for run.process_pdf().

    Same call shape as the LLM pipeline / v1_computer_vision / v2_computer_vision:
        process_pdf(pdf_path, client, registry, *, pages_dir, want, auto_build,
                    on_page) -> (results, metrics)
    with on_page(page_number, page_image_path, result) invoked once per page,
    exactly like the existing pipeline — this is what lets api.py's on_page
    hook (per-page blob upload + Postgres persist) work unchanged.

    `registry` and `auto_build` are accepted only for signature compatibility
    and are unused: this pipeline has exactly one locked template, not a
    catalog registry, and never auto-builds new templates. `client` is
    accepted for injectability; if omitted, one is built via this package's
    own model_client.make_client(). All detection/extraction/rendering/AI
    mark-review logic is delegated to render.py, locked_template.py,
    extract.py, mark_resolver.py, and dates.py, unchanged.
    """
    s = get_settings()
    manifest, _, _ = load_manifest_and_catalog()
    template = locked_template()

    pdf_bytes = Path(pdf_path).read_bytes()
    pages = render_pdf(pdf_bytes, s.render_dpi, wanted=set(want) if want else None)
    logger.info("CV pipeline: %s -> %d page(s) to process (template=%s)",
                os.path.basename(pdf_path), len(pages), s.template_id)

    needs_client = client is not None or s.enable_header_notes or (s.enable_mark_ai and not no_mark_ai)
    active_client = client if client is not None else (_make_client() if needs_client else None)

    results = []
    for rp in pages:
        page_image_path = _write_page_png(pages_dir, rp.page_number, rp.png_bytes)

        bgr = cv2.imdecode(np.frombuffer(rp.png_bytes, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError(f"Rendered page {rp.page_number} could not be decoded")

        alignment = template.align(bgr)

        if not template.is_match(alignment):
            result = _mismatch_result(rp.page_number, alignment, template)
            logger.info(
                "page %2d: [mismatch] match=%.3f — skipped (locked_template_mismatch)",
                rp.page_number, alignment.match_score,
            )
        else:
            procedures, diagnoses, possible_marks, geometry = template.detect(alignment)

            # Constrained AI candidate review — can only select A/B/C labels
            # mapping back to deterministic candidates already in
            # possible_marks; see mark_resolver.py's own safety-boundary
            # docstring. Cached by page-sha + possible_marks fingerprint +
            # model + resolver version, same pattern as the header cache.
            suppressed_marks = []
            mark_ai_summary = {
                "status": "disabled" if (no_mark_ai or not s.enable_mark_ai) else "not_needed",
                "promoted_codes": [], "promoted_count": 0, "suppressed_count": 0,
                "remaining_possible_count": len(possible_marks),
            }
            mark_ai_source = "disabled"
            if possible_marks and not no_mark_ai and s.enable_mark_ai:
                mp = _v221_run._mark_cache_path(rp.sha256, possible_marks)
                if mp.exists():
                    mark_review = json.loads(mp.read_text(encoding="utf-8"))
                    mark_ai_source = "cache"
                else:
                    mark_review = review_possible_marks(alignment.color, possible_marks, template, active_client)
                    mp.write_text(json.dumps(mark_review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    mark_ai_source = "model"
                procedures, diagnoses, possible_marks, suppressed_marks, mark_ai_summary = apply_mark_review(
                    procedures, diagnoses, possible_marks, mark_review, template
                )
                mark_ai_summary["source"] = mark_ai_source

            manual_review_procedures, manual_review_diagnoses = _v221_run._manual_review_code_buckets(
                possible_marks, template, procedures
            )

            if not s.enable_header_notes:
                header_notes = {"header": {}, "notes": [], "flags": ["header_notes_disabled"]}
                header_source = "disabled"
            else:
                cache_path = _v221_run._header_cache_path(rp.sha256)
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

            result = _matched_result(
                rp.page_number, rp.sha256, alignment, template, manifest,
                header_notes, header_source, procedures, diagnoses,
                manual_review_procedures, manual_review_diagnoses,
                possible_marks, suppressed_marks, mark_ai_summary, mark_ai_source,
                geometry,
            )
            header = result.get("header") or {}
            logger.info(
                "page %2d: %-28s [locked] proc=%d(review=%d) dx=%d(review=%d) "
                "possible=%d suppressed=%d notes=%d match=%.3f mark_ai=%s header=%s "
                "flags=%s",
                rp.page_number, header.get("name", "?"),
                len(result.get("circled_procedures", [])), len(manual_review_procedures),
                len(result.get("circled_diagnoses", [])), len(manual_review_diagnoses),
                len(result.get("possible_marks", [])), len(suppressed_marks),
                len(result.get("notes", [])), alignment.match_score, mark_ai_source,
                header_source, result.get("flags", []),
            )

        results.append(result)
        if on_page:
            on_page(rp.page_number, page_image_path, result)

    metrics = build_metrics(pdf_path, results)
    logger.info(
        "CV pipeline done: %s -> %d page(s), %d extracted, %d not-chargesheet, "
        "%d procedures, %d diagnoses, %d manual-review procs, %d manual-review dx, "
        "%d possible_marks, %d notes",
        os.path.basename(pdf_path), metrics["pages_processed"], metrics["pages_extracted"],
        metrics["pages_not_chargesheet"], metrics["total_procedures"],
        metrics["total_diagnoses"], metrics["total_manual_review_procedures"],
        metrics["total_manual_review_diagnoses"], metrics["total_possible_marks"],
        metrics["total_notes"],
    )
    return results, metrics
