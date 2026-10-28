"""
pipeline_adapter.py — format-compatibility wrapper, NOT a reimplementation.

Every actual piece of detection/localization/adjudication/rendering/header
logic here is delegated unchanged to this package's own modules (run_v3.py's
process_page(), handwriting.py, mark_localizer.py, mark_adjudicator.py,
page_audit.py, alignment.py, header_extract.py, dates.py). This file only
reshapes the call surface so the v3 mark-centric pipeline can be invoked with
the exact same function signature and return shape as every prior pipeline
this repo has integrated —
    run.process_pdf(pdf_path, client, registry, *, pages_dir, want, auto_build,
                    on_page) -> (results, metrics)
— a drop-in swap for callers (api.py) with no changes needed on their side.

v3's own run_v3.py is a CLI script, not a library entry point with a
pages_dir/on_page hook — this adapter drives run_v3.process_page() once per
rendered page in the same order main() does (render -> align -> handwriting
isolate -> localize marks -> adjudicate -> audit -> header/notes -> populate
outputs), so on_page can still be invoked once per page like every other
pipeline this repo has integrated. Nothing here changes core detection or
adjudication logic.

One small compatibility shim IS applied here (see _postprocess_page): v3 uses
its own flag vocabulary ("template_match_below_threshold") for a page that
fails to match the locked template, which does not overlap with the legacy
extraction_flags.ERRORED_CHILD_FLAGS trio db.persist_page_v2 checks to decide
a child attachment's status. This adapter adds those legacy flags alongside
v3's own flag on a mismatched page so that status-marking keeps working,
without touching run_v3.py's own logic.
"""
from __future__ import annotations

import datetime
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

logger = logging.getLogger("chargesheet.v3_4_computer_vision")

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # so this package's own modules resolve
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
    """Load this package's own {module_filenames}.py, even though the main
    repo has its own same-named auth.py (DB credentials) that may already be
    cached in sys.modules — which would otherwise silently win when run_v3.py
    (or model_client.py's lazy `from auth import ...` fallback) does a bare
    `import auth`/`from auth import ...` and break this package's own
    Key-Vault-to-Anthropic-Foundry credential path. Hides any conflicting
    cache entries for the whole load, then restores them, so no other
    importer in the process is affected."""
    saved = {name: sys.modules.pop(name, None) for name in module_filenames}
    try:
        return tuple(_exec_own_module(name) for name in module_filenames)
    finally:
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod
            else:
                sys.modules.pop(name, None)


# run_v3.py pulls in this package's own auth/render/settings/dates/
# template_registry/alignment/model_client/header_extract/handwriting/
# mark_localizer/mark_adjudicator/page_audit modules via bare imports.
# auth.py in particular collides by name with the main repo's own auth.py
# (Postgres/Key-Vault DB credentials) — load the whole set isolated so those
# imports resolve to this package's copies, not whatever is already cached.
_v3_modules = _load_own_modules_isolated(
    "auth", "render", "settings", "dates", "template_registry", "alignment",
    "model_client", "header_extract", "handwriting", "mark_localizer",
    "mark_adjudicator", "page_audit", "run_v3",
)
_v3_run = _v3_modules[-1]


def _write_page_png(pages_dir: str, page_number: int, png_bytes: bytes) -> str:
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"page-{page_number:02d}.png")
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def _postprocess_page(page: dict) -> None:
    """Reshape v3's page dict for compatibility with legacy consumers.

    Adds the legacy not_chargesheet/skipped_no_extraction/blank_header flag
    trio on a template mismatch (v3 only sets its own
    template_match_below_threshold flag, which extraction_flags.
    ERRORED_CHILD_FLAGS does not recognize), and a `recognition` block mirror
    of `template_match` for callers still reading that older field name.
    Does not touch confirmed/manual_review/rejected content.
    """
    template_ok = bool(page.get("template_ok"))
    template_match = page.get("template_match") or {}
    score = template_match.get("score", 0.0)

    if not template_ok:
        flags = list(page.get("flags") or [])
        for f in (FLAG_NOT_CHARGESHEET, FLAG_SKIPPED_NO_EXTRACTION, FLAG_BLANK_HEADER):
            if f not in flags:
                flags.append(f)
        page["flags"] = flags

    page["recognition"] = {
        "is_chargesheet": template_ok,
        "confidence": score,
        "method": "locked_template_registration",
    }


def _flag_counts(results: list) -> dict:
    counts: dict = {}
    for r in results:
        for flag in r.get("flags", []) or []:
            counts[flag] = counts.get(flag, 0) + 1
    return counts


def orientation_info(results: list) -> dict:
    dist: dict = {}
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


def build_metrics(pdf_path: str, results: list) -> dict:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    extracted = sum(1 for r in results if r.get("template_ok"))
    not_cs = len(results) - extracted
    return {
        "timestamp": ts,
        "source_pdf": os.path.basename(pdf_path),
        "orientation": orientation_info(results),
        "pages_processed": len(results),
        "pages_extracted": extracted,
        "pages_not_chargesheet": not_cs,
        "pages_flagged": sum(1 for r in results if r.get("flags")),
        "pages_autobuilt": 0,
        "total_procedures": sum(len(r.get("circled_procedures", [])) for r in results),
        "total_diagnoses": sum(len(r.get("circled_diagnoses", [])) for r in results),
        "total_manual_review_procedures": sum(
            len(r.get("manual_review_procedures", [])) for r in results),
        "total_manual_review_diagnoses": sum(
            len(r.get("manual_review_diagnoses", [])) for r in results),
        "total_notes": sum(len(r.get("notes", [])) for r in results),
        "total_marks": sum(len(r.get("marks", [])) for r in results),
        "total_rejected_marks": sum(len(r.get("rejected_marks", [])) for r in results),
        "flag_counts": _flag_counts(results),
        "pipeline": "v3_4_computer_vision",
    }


def process_pdf(pdf_path: str, client=None, registry=None, *, pages_dir: str = "pages",
                want=None, auto_build: bool = True, on_page=None,
                no_mark_ai: bool = False):
    """Drop-in, format-compatible replacement for run.process_pdf().

    Same call shape as every other pipeline integrated in this repo:
        process_pdf(pdf_path, client, registry, *, pages_dir, want, auto_build,
                    on_page) -> (results, metrics)
    with on_page(page_number, page_image_path, result) invoked once per page.

    `registry` and `auto_build` are accepted only for signature compatibility
    and are unused: this pipeline has exactly one locked template, not a
    catalog registry, and never auto-builds new templates. `client` is
    accepted for injectability; if omitted, one is built via this package's
    own model_client.make_client(). All detection/localization/adjudication/
    rendering logic is delegated to handwriting.py, mark_localizer.py,
    mark_adjudicator.py, page_audit.py, alignment.py, header_extract.py, and
    run_v3.process_page(), unchanged.
    """
    s = _v3_run.get_settings()
    _, catalog, _ = _v3_run.load_manifest_and_catalog()
    template = _v3_run.locked_template()
    glyphs = _v3_run.handwriting.glyph_boxes(template.ref, catalog["cells"])

    pdf_bytes = Path(pdf_path).read_bytes()
    pages = _v3_run.render_pdf(pdf_bytes, dpi=s.render_dpi, wanted=set(want) if want else None)
    logger.info("CV pipeline: %s -> %d page(s) to process (template=%s)",
                os.path.basename(pdf_path), len(pages), s.template_id)

    active_client = client if client is not None else (None if no_mark_ai else _v3_run.make_client())

    # run_v3.process_page() reads a handful of argparse-style attributes off
    # `args`; build a minimal stand-in rather than pulling in argparse here.
    args = types.SimpleNamespace(
        dump_crops=None,
        no_header=False,
        refresh_header_cache=False,
        no_audit=False,
        page_dir=pages_dir,
        out=os.path.join(pages_dir, "run.json"),
        model=None,
    )

    results = []
    for rp in pages:
        page = _v3_run.process_page(rp, template, catalog, glyphs, active_client,
                                    s.mark_model, s, args)
        _postprocess_page(page)

        header = page.get("header") or {}
        logger.info(
            "page %2d: %-28s [v3.4] proc=%d(review=%d) dx=%d(review=%d) "
            "marks=%d rejected=%d notes=%d match=%.3f header=%s flags=%s",
            rp.page_number, header.get("name", "?"),
            len(page.get("circled_procedures", [])), len(page.get("manual_review_procedures", [])),
            len(page.get("circled_diagnoses", [])), len(page.get("manual_review_diagnoses", [])),
            len(page.get("marks", [])), len(page.get("rejected_marks", [])),
            len(page.get("notes", [])), page.get("template_match", {}).get("score", 0.0),
            page.get("header_notes_source", "n/a"), page.get("flags", []),
        )

        results.append(page)
        if on_page:
            page_image_path = _write_page_png(pages_dir, rp.page_number, rp.png_bytes)
            on_page(rp.page_number, page_image_path, page)

    metrics = build_metrics(pdf_path, results)
    logger.info(
        "CV pipeline done: %s -> %d page(s), %d extracted, %d not-chargesheet, "
        "%d procedures, %d diagnoses, %d manual-review procs, %d manual-review dx, "
        "%d marks, %d rejected, %d notes",
        os.path.basename(pdf_path), metrics["pages_processed"], metrics["pages_extracted"],
        metrics["pages_not_chargesheet"], metrics["total_procedures"],
        metrics["total_diagnoses"], metrics["total_manual_review_procedures"],
        metrics["total_manual_review_diagnoses"], metrics["total_marks"],
        metrics["total_rejected_marks"], metrics["total_notes"],
    )
    return results, metrics
