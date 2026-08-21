"""Charge-sheet extraction runner — v3 mark-centric architecture (standalone).

    python run_v3.py July-02.pdf --pages 6,19,21 --out july02-v3.json
    python run_v3.py July-02.pdf --pages 6 --no-ai --dump-crops ./crops
    python run_v3.py July-02.pdf --out full.json --no-audit

This package is self-contained. It does not import from chargesheet_extraction_v2.

Pipeline, per page:
  1  render (pdftoppm) + align to the locked reference     alignment.py
  2  isolate handwriting via the clearance gate            handwriting.py
  3  group into discrete marks + locked candidate rows     mark_localizer.py
  4  resolve each mark from its own crop                   mark_adjudicator.py
  5  independent full-page recall sweep                    page_audit.py
  6  reconcile, enforce invariants, emit JSON

Billing-code identity is locked-coordinates-only. The reader sees crops and
returns indices into candidate lists that Python built from catalog.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np

import handwriting
import mark_localizer
import page_audit
from dates import normalize_header_dates
from header_extract import extract_header_notes
from mark_adjudicator import adjudicate, reconcile
from model_client import make_client
from render import render_pdf
from settings import get_settings
from template_registry import load_manifest_and_catalog, locked_template

HERE = Path(__file__).resolve().parent


def _cache_path(kind: str, key: str) -> Path:
    p = HERE / "runtime" / f"{kind}_cache_v3" / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _page_output_dir(args) -> Path:
    if getattr(args, "page_dir", None):
        return Path(args.page_dir)
    out = Path(args.out)
    return out.parent / f"{out.stem}_pages"


def _relative_output_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except Exception:
        return str(path)


def _header_notes(client, alignment, page_sha: str, s, refresh: bool) -> tuple[dict, str]:
    """v2-compatible header/notes extraction, cached per page image."""
    cp = _cache_path("header", _key(page_sha, s.header_model, s.pipeline_version))
    if cp.exists() and not refresh:
        return json.loads(cp.read_text(encoding="utf-8")), "cache"
    rgb = cv2.cvtColor(alignment.color, cv2.COLOR_BGR2RGB)
    hn = extract_header_notes(rgb, client)
    cp.write_text(json.dumps(hn, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return hn, "model"


def _populate_code_outputs(page: dict, seen_confirmed: dict[str, dict],
                           seen_review: dict[str, dict], catalog: dict) -> None:
    """Populate final code buckets without mixing review into downstream output."""
    for code in list(seen_review):
        if code in seen_confirmed:
            seen_review.pop(code)

    by_code = {c["code"]: c for c in catalog["cells"]}
    confirmed = sorted(seen_confirmed.values(), key=lambda c: c["code"])
    review = sorted(seen_review.values(), key=lambda c: c["code"])

    page["circled_procedures"] = [
        c for c in confirmed if by_code[c["code"]].get("kind") == "procedure"
    ]
    page["circled_diagnoses"] = [
        c for c in confirmed if by_code[c["code"]].get("kind") != "procedure"
    ]
    page["manual_review_procedures"] = [
        c for c in review if by_code[c["code"]].get("kind") == "procedure"
    ]
    page["manual_review_diagnoses"] = [
        c for c in review if by_code[c["code"]].get("kind") != "procedure"
    ]

    # Hard downstream contract: confirmed procedures only.
    page["procedure_codes"] = [
        {**c, "status": "confirmed"} for c in page["circled_procedures"]
    ]
    page["procedure_summary"] = {
        "confirmed_count": len(page["circled_procedures"]),
        "manual_review_count": len(page["manual_review_procedures"]),
        "surfaced_count": (len(page["circled_procedures"])
                            + len(page["manual_review_procedures"])),
        "procedure_codes_count": len(page["procedure_codes"]),
        "invariant_ok": True,
    }


def process_page(rendered, template, catalog, glyphs, client, model, s, args) -> dict:
    arr = np.frombuffer(rendered.png_bytes, np.uint8)
    color = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if color is None:
        return {"page": rendered.page_number, "template_ok": False,
                "flags": ["page_image_unreadable"]}

    alignment = template.align(color)
    matched = template.is_match(alignment)
    gray = alignment.gray

    # Persist both the source page slice and the normalized/aligned processing
    # image. These are operational artifacts, not debug-only crops.
    page_dir = _page_output_dir(args)
    page_dir.mkdir(parents=True, exist_ok=True)
    source_page_path = page_dir / f"page-{rendered.page_number:03d}-source.png"
    aligned_page_path = page_dir / f"page-{rendered.page_number:03d}-aligned.png"
    source_page_path.write_bytes(rendered.png_bytes)
    cv2.imwrite(str(aligned_page_path), gray)

    layer = handwriting.isolate(gray, template.ref)
    marks = mark_localizer.find_marks(layer, catalog, glyphs)
    table_marks = [m for m in marks if m.band == "table"]

    if args.dump_crops:
        out = Path(args.dump_crops) / f"page-{rendered.page_number:03d}"
        out.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out / "aligned.png"), gray)
        cv2.imwrite(str(out / "handwriting.png"), layer.strokes * 255)
        for m in table_marks:
            (out / f"mark-{m.id:02d}.png").write_bytes(mark_localizer.render_crop(gray, m))
            (out / f"mark-{m.id:02d}.json").write_text(json.dumps({
                "bbox": m.bbox, "stroke_px": m.stroke_px,
                "max_clearance_px": m.max_clearance,
                "candidates": [{"index": i, "code": c["code"],
                                "description": c["description"]}
                               for i, c in enumerate(m.candidates)],
                "geometry_hint": m.hint[:4],
            }, indent=2))

    page: dict = {
        "page": rendered.page_number,
        "page_sha256": rendered.sha256,
        "template_ok": bool(matched),
        "header": {},
        "circled_procedures": [],
        "circled_diagnoses": [],
        "manual_review_procedures": [],
        "manual_review_diagnoses": [],
        "procedure_codes": [],
        "marks": [],
        "rejected_marks": [],
        "notes": [],
        "flags": [],
        "handwriting_layer": layer.stats,
        "localizer": {
            "marks_total": len(marks),
            "marks_in_table_band": len(table_marks),
            "marks_write_in": sum(1 for m in marks if m.band == "write_in"),
            "marks_header_footer": sum(1 for m in marks
                                       if m.band in ("header", "footer")),
            "marks_oversized_for_a_loop": [m.id for m in table_marks if m.oversized],
        },
        "template_match": {
            "state": "locked" if matched else "unmatched",
            "score": round(float(alignment.match_score), 4),
            "orb_inlier_ratio": round(float(alignment.orb_inlier_ratio), 4),
            "template_id": catalog.get("template_id"),
        },
        "page_images": {
            "source": _relative_output_path(source_page_path),
            "aligned": _relative_output_path(aligned_page_path),
        },
        "orientation": {
            "detected": True,
            "applied_rotation_deg": alignment.rotation_ccw,
            "result": "upright",
            "method": "locked_template_registration",
        },
        "pipeline": s.pipeline_version,
    }

    if not matched:
        page["flags"].append("template_match_below_threshold")
        return page

    if client is None:
        page["flags"].append("ai_disabled_geometry_only")
        for m in table_marks:
            page["marks"].append({
                "mark_id": m.id, "bbox": m.bbox, "stroke_px": m.stroke_px,
                "max_clearance_px": m.max_clearance,
                "candidates": [c["code"] for c in m.candidates],
                "geometry_hint": m.hint[:4],
                "status": "unresolved_ai_disabled",
            })
        return page

    # ---- 4. per-mark crop adjudication -------------------------------------
    resolved = []
    for m in table_marks:
        png = mark_localizer.render_crop(gray, m)
        decision = adjudicate(client, model, m, png)
        resolved.append(reconcile(m, decision, s.promote_confidence, s.hint_margin, s.min_physical_coverage))

    # ---- 5. independent recall sweep ---------------------------------------
    audit_stats: dict = {"skipped": True}
    if s.enable_page_audit and not args.no_audit:
        recovered, audit_stats = page_audit.find_missed_marks(
            client, model, gray, catalog, glyphs, table_marks)
        for m in recovered:
            png = mark_localizer.render_crop(gray, m)
            decision = adjudicate(client, model, m, png)
            r = reconcile(m, decision, s.promote_confidence, s.hint_margin, s.min_physical_coverage)
            r["source"] = "page_audit_recovered"
            for c in r["confirmed"]:
                c["resolution"] = "page_audit_recovered"
            resolved.append(r)
    page["page_audit"] = audit_stats

    # ---- 6. reconcile ------------------------------------------------------
    seen_confirmed: dict[str, dict] = {}
    seen_review: dict[str, dict] = {}
    for r in resolved:
        page["marks"].append({k: v for k, v in r.items() if k != "mask"})
        if r["no_selection"]:
            reason = {
                "no_candidate_rows": "no_locked_row_within_range",
                "unparseable": "reader_response_unparseable",
            }.get(r.get("reader_status"), "reader_found_no_selection")
            page["rejected_marks"].append({
                "mark_id": r["mark_id"], "bbox": r["bbox"],
                "stroke_px": r["stroke_px"],
                "max_clearance_px": r["max_clearance_px"],
                "reason": reason,
                "other_marks": r["other_marks"],
            })
        for c in r["confirmed"]:
            prev = seen_confirmed.get(c["code"])
            if prev is None or c.get("confidence", 0) > prev.get("confidence", 0):
                seen_confirmed[c["code"]] = {**c, "mark_id": r["mark_id"],
                                             "detection": "crop_visual_locked_universe"}
        for c in r["manual_review"]:
            if c["code"] not in seen_review:
                seen_review[c["code"]] = {**c, "mark_id": r["mark_id"],
                                          "status": "manual_review"}
        for x in r["other_marks"]:
            page["notes"].append({"text": x, "near": f"mark {r['mark_id']}",
                                  "source": "crop_reader"})
        if r["invalid_indexes_rejected"]:
            page["flags"].append("reader_returned_invalid_index")

    _populate_code_outputs(page, seen_confirmed, seen_review, catalog)

    # v3's invariant is accounting, not "a procedure must exist". A localized
    # mark may legitimately be a diagnosis, another handwritten mark, or a
    # rejected/no-selection crop. We never manufacture a procedure to avoid 0.
    if page["manual_review_procedures"]:
        page["flags"].append("manual_code_review_required")
    if page["rejected_marks"]:
        page["flags"].append("marks_rejected_by_reader")

    # ---- header / notes ----------------------------------------------------
    if not args.no_header and s.enable_header_notes:
        try:
            hn, source = _header_notes(client, alignment, rendered.sha256, s,
                                       args.refresh_header_cache)
            header = hn.get("header") or {}
            _, _, date_flags = normalize_header_dates(header)
            page["header"] = header
            page["notes"].extend(hn.get("notes") or [])
            page["flags"].extend(list(hn.get("flags") or []) + date_flags)
            page["header_notes_source"] = source
        except Exception as exc:
            page["flags"].append("header_extraction_failed")
            page["header_error"] = str(exc)[:200]
    else:
        page["flags"].append("header_notes_disabled")

    return page


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Charge-sheet extraction v3")
    ap.add_argument("pdf")
    ap.add_argument("--pages", help="comma separated 1-based page numbers")
    ap.add_argument("--out", default="chargesheet_v3.json")
    ap.add_argument("--no-ai", action="store_true",
                    help="localize only; emit marks/candidates without resolving")
    ap.add_argument("--no-audit", action="store_true",
                    help="skip the full-page recall sweep")
    ap.add_argument("--no-header", action="store_true",
                    help="skip header/notes extraction")
    ap.add_argument("--refresh-header-cache", action="store_true")
    ap.add_argument("--dump-crops", help="directory for crops + debug images")
    ap.add_argument("--page-dir", help="directory for persistent source/aligned page PNGs; defaults to <out-stem>_pages")
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)

    s = get_settings()
    _, catalog, _ = load_manifest_and_catalog()
    template = locked_template()
    glyphs = handwriting.glyph_boxes(template.ref, catalog["cells"])

    wanted = None
    if args.pages:
        wanted = {int(x) for x in args.pages.replace(" ", "").split(",") if x}

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")
    pdf_bytes = pdf_path.read_bytes()
    pages = render_pdf(pdf_bytes, dpi=s.render_dpi, wanted=wanted)

    client = None if args.no_ai else make_client()
    model = args.model or s.mark_model

    processed_pages = [process_page(p, template, catalog, glyphs, client, model, s, args)
                       for p in pages]
    rotation_counts: dict[str, int] = {}
    for pg in processed_pages:
        rot = str((pg.get("orientation") or {}).get("applied_rotation_deg", "unknown"))
        rotation_counts[rot] = rotation_counts.get(rot, 0) + 1

    out = {
        "source_pdf": pdf_path.name,
        "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "page_count": len(pages),
        "pipeline": s.pipeline_version,
        "template_id": catalog.get("template_id"),
        "template_version": s.template_version,
        "page_storage": {
            "directory": _relative_output_path(_page_output_dir(args)),
            "source_and_aligned_pngs_persisted": True,
        },
        "orientation": {
            "detected": True,
            "method": "locked_template_registration",
            "result": "upright_after_alignment",
            "applied_rotation_deg_distribution": rotation_counts,
        },
        "safety": {
            "code_universe": "locked_catalog_rows_only",
            "ai_can_invent_codes": False,
            "ai_output_identity": "candidate_list_indices_only",
            "ai_sees_crops_not_scores": True,
            "page_audit_returns_coordinates_only": True,
            "marks_never_silently_dropped": True,
            "geometry_is_corroboration_not_decision": True,
            "physical_overlap_minimum": s.min_physical_coverage,
            "adjacent_touch_alone_never_confirms": True,
            "physical_complete_circle_material_overlap_confirms": True,
            "physical_incomplete_arc_visual_majority_supported": True,
            "nonphysical_residual_never_confirms": True,
            "procedure_codes_confirmed_only": True,
        },
        "pages": processed_pages,
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    for p in out["pages"]:
        codes = ([c["code"] for c in p.get("circled_procedures", [])]
                 + [c["code"] for c in p.get("circled_diagnoses", [])])
        rev = [c["code"] for c in p.get("manual_review_procedures", [])]
        print(f"  page {p['page']:>3}  confirmed={codes or '-'}  review={rev or '-'}  "
              f"marks={p.get('localizer', {}).get('marks_in_table_band', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
