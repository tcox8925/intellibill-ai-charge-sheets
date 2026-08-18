"""Charge-sheet extraction runner — v2.2.1 hybrid candidate architecture.

Billing-code universe remains locked/deterministic.  The visual model may only
review candidates that locked_template.py already produced from physical ink at
known coordinates; it cannot invent or name a code outside that candidate set.

Examples:
  python run.py input.pdf --out results.json
  python run.py input.pdf --pages 1,4,5 --out spot.json
  python run.py input.pdf --no-ai --out geometry_only.json
  python run.py input.pdf --no-mark-ai --out no_mark_review.json
  python run.py input.pdf --refresh-ai-cache --out fresh.json

Normal internal auth path: run `az login`, then run this command. auth.py uses the
shared Key Vault + Anthropic Foundry configuration from the earlier pipeline.
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
from mark_resolver import apply_mark_review, review_possible_marks
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


def _mark_cache_path(page_sha: str, possible_marks: list[dict]) -> Path:
    s = get_settings()
    fingerprint = json.dumps(possible_marks, sort_keys=True, separators=(",", ":"), default=str)
    key = hashlib.sha256(
        f"{page_sha}|{hashlib.sha256(fingerprint.encode()).hexdigest()}|{s.mark_model}|{s.mark_resolver_version}".encode("utf-8")
    ).hexdigest()
    p = Path(__file__).resolve().parent / "runtime" / "mark_cache" / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _empty_page(page_number: int, score: float, template_id: str, rotation: int | None) -> dict:
    return {
        "page": page_number,
        "template_ok": False,
        "header": {},
        "circled_procedures": [],
        "circled_diagnoses": [],
        "manual_review_procedures": [],
        "manual_review_diagnoses": [],
        "procedure_codes": [],
        "procedure_summary": {
            "confirmed_count": 0,
            "manual_review_count": 0,
            "surfaced_count": 0,
            "invariant_ok": False,
        },
        "possible_marks": [],
        "suppressed_marks": [],
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


def _cell_by_code(template, code: str):
    for cell in template.cells:
        if str(cell.get("code")) == str(code):
            return cell
    return None


def _candidate_score(c: dict) -> float:
    try:
        return float(c.get("score") or 0.0)
    except Exception:
        return 0.0


def _candidate_confirmation_level(template, c: dict) -> bool:
    source = str(c.get("source") or "")
    threshold = float(template.thresholds[
        "color_confirm_min" if source == "color_geometry" else "black_confirm_min"
    ])
    return _candidate_score(c) >= threshold


def _manual_review_code_buckets(possible_marks: list[dict], template, confirmed_procedures: list[dict]):
    """Surface real adjacent-overlap codes separately from noisy possible marks.

    possible_marks remains available for audit/debug, but downstream callers can
    ignore it. Adjacent overlaps that were not safely promoted are copied into
    explicit manual-review procedure/diagnosis buckets. If a matched charge sheet
    has no confirmed procedure, the strongest Office Services candidate group is
    surfaced for manual review so the page never silently reports zero procedure
    codes when procedure evidence exists. No code can be invented: every surfaced
    code must already exist in the locked candidate list.
    """
    s = get_settings()
    manual_proc: list[dict] = []
    manual_dx: list[dict] = []
    seen_proc = {str(x.get("code")) for x in confirmed_procedures}
    seen_manual_proc: set[str] = set()
    seen_manual_dx: set[str] = set()

    def add_from_mark(mark: dict, reason: str):
        ai = mark.get("ai_review") or {}
        selected = {str(x) for x in (ai.get("candidate_codes_selected") or [])}
        candidates = list(mark.get("candidate_codes") or [])[: max(2, s.mark_display_candidates)]
        for c in candidates:
            code = str(c.get("code") or "")
            if not code:
                continue
            cell = _cell_by_code(template, code)
            if cell is None:
                continue
            kind = str(cell.get("kind") or mark.get("kind") or "")
            item = {
                "code": code,
                "description": str(cell.get("description") or ""),
                "section": str(cell.get("section") or ""),
                "status": "manual_review",
                "reason": reason,
                "deterministic_candidate_score": round(_candidate_score(c), 4),
                "ai_classification": ai.get("classification"),
                "ai_confidence": ai.get("confidence"),
                "ai_suggested": code in selected,
            }
            if kind == "procedure":
                if code in seen_proc or code in seen_manual_proc:
                    continue
                seen_manual_proc.add(code)
                manual_proc.append(item)
            elif kind == "diagnosis":
                if code in seen_manual_dx:
                    continue
                seen_manual_dx.add(code)
                manual_dx.append(item)

    # Normal manual-review path: meaningful adjacent overlaps only.
    for mark in possible_marks:
        if str(mark.get("reason") or "") != "adjacent_code_overlap":
            continue
        ai = mark.get("ai_review") or {}
        cls = str(ai.get("classification") or "")
        reviewed_mark = cls in {"circle_single", "circle_multiple", "ambiguous", "scribble"}
        # If AI explicitly says NONE, do not turn that noisy overlap into a manual
        # coding task. If the group was never AI-reviewed, leave it in possible
        # unless the page-level zero-procedure invariant needs a fallback below.
        if reviewed_mark:
            add_from_mark(mark, "adjacent_code_overlap")

    # Business invariant: a valid charge sheet always has a procedure. If none
    # was safely confirmed or surfaced above, expose the strongest procedure
    # candidate group as MANUAL REVIEW rather than returning proc=0. Prefer
    # Office Services because that is the primary visit-code area on this form.
    if not confirmed_procedures and not manual_proc:
        fallback = []
        for mark in possible_marks:
            proc_candidates = []
            office = False
            for c in mark.get("candidate_codes") or []:
                cell = _cell_by_code(template, str(c.get("code") or ""))
                if cell is None or str(cell.get("kind") or "") != "procedure":
                    continue
                proc_candidates.append(c)
                office = office or str(cell.get("section") or "") == "Office Services"
            if not proc_candidates:
                continue
            fallback.append((1 if office else 0, max(_candidate_score(c) for c in proc_candidates), mark))
        if fallback:
            fallback.sort(key=lambda x: (x[0], x[1]), reverse=True)
            add_from_mark(fallback[0][2], "procedure_required_manual_review")

    return manual_proc, manual_dx


def process_pdf(pdf_path: str, *, wanted: set[int] | None = None, no_ai: bool = False,
                no_mark_ai: bool = False, refresh_ai_cache: bool = False,
                refresh_header_cache: bool = False) -> dict:
    s = get_settings()
    manifest, _, _ = load_manifest_and_catalog()
    template = locked_template()

    pdf = Path(pdf_path)
    pdf_bytes = pdf.read_bytes()
    pages = render_pdf(pdf_bytes, s.render_dpi, wanted=wanted)
    needs_client = not no_ai and (
        s.enable_header_notes or (s.enable_mark_ai and not no_mark_ai)
    )
    client = make_client() if needs_client else None

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
                f"page {rp.page_number:>3}: [mismatch] proc=0 dx=0 possible=0 notes=0 "
                f"match={alignment.match_score:.3f}"
            )
            continue

        procedures, diagnoses, possible_marks, geometry = template.detect(alignment)

        # Constrained AI candidate review.  It can only select A/B/C labels that
        # map back to deterministic candidates already present in possible_marks.
        suppressed_marks = []
        mark_ai_summary = {
            "status": "disabled" if (no_ai or no_mark_ai or not s.enable_mark_ai) else "not_needed",
            "promoted_codes": [], "promoted_count": 0, "suppressed_count": 0,
            "remaining_possible_count": len(possible_marks),
        }
        mark_ai_source = "disabled"
        if possible_marks and not no_ai and not no_mark_ai and s.enable_mark_ai:
            mp = _mark_cache_path(rp.sha256, possible_marks)
            if mp.exists() and not refresh_ai_cache:
                mark_review = json.loads(mp.read_text(encoding="utf-8"))
                mark_ai_source = "cache"
            else:
                mark_review = review_possible_marks(alignment.color, possible_marks, template, client)
                mp.write_text(json.dumps(mark_review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                mark_ai_source = "model"
            procedures, diagnoses, possible_marks, suppressed_marks, mark_ai_summary = apply_mark_review(
                procedures, diagnoses, possible_marks, mark_review, template
            )
            mark_ai_summary["source"] = mark_ai_source

        manual_review_procedures, manual_review_diagnoses = _manual_review_code_buckets(
            possible_marks, template, procedures
        )
        surfaced_procedure_codes = []
        for item in list(procedures) + list(manual_review_procedures):
            code = str(item.get("code") or "")
            if code and code not in surfaced_procedure_codes:
                surfaced_procedure_codes.append(code)

        if no_ai or not s.enable_header_notes:
            hn = {"header": {}, "notes": [], "flags": ["header_notes_disabled"]}
            header_source = "disabled"
        else:
            cp = _header_cache_path(rp.sha256)
            if cp.exists() and not refresh_header_cache and not refresh_ai_cache:
                hn = json.loads(cp.read_text(encoding="utf-8"))
                header_source = "cache"
            else:
                rgb = cv2.cvtColor(alignment.color, cv2.COLOR_BGR2RGB)
                hn = extract_header_notes(rgb, client)
                cp.write_text(json.dumps(hn, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                header_source = "model"

        header = hn.get("header") or {}
        _, _, date_flags = normalize_header_dates(header)
        flags = list(hn.get("flags") or []) + date_flags + ["locked_coordinate_candidate_source"]
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
        flags = list(dict.fromkeys(str(x) for x in flags if str(x).strip()))

        result = {
            "page": rp.page_number,
            "page_sha256": rp.sha256,
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
            "notes": hn.get("notes") or [],
            "flags": flags,
            "header_notes_source": header_source,
            "mark_ai": mark_ai_summary,
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
            f"page {rp.page_number:>3}: [locked] proc={len(surfaced_procedure_codes)} "
            f"(confirmed={len(procedures)} review={len(manual_review_procedures)}) "
            f"dx={len(diagnoses)} dx_review={len(manual_review_diagnoses)} "
            f"possible={len(possible_marks)} suppressed={len(suppressed_marks)} "
            f"notes={len(result['notes'])} match={alignment.match_score:.3f} "
            f"mark_ai={mark_ai_source} header={header_source}"
        )

    return {
        "source_pdf": pdf.name,
        "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "page_count": len(results),
        "pipeline": s.pipeline_version,
        "template_id": s.template_id,
        "template_version": s.template_version,
        "safety": {
            "code_universe": "locked_catalog_candidates_only",
            "ai_can_invent_codes": False,
            "ambiguous_candidates_are_preserved": True,
            "manual_review_codes_are_separate_from_confirmed": True,
            "procedure_zero_is_not_a_valid_silent_result": True,
        },
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
    ap.add_argument("--no-ai", action="store_true", help="geometry only; no visual/header model calls")
    ap.add_argument("--no-mark-ai", action="store_true", help="keep header/notes AI but disable candidate mark AI")
    ap.add_argument(
        "--refresh-header-cache",
        action="store_true",
        help="re-run header/notes model instead of reusing cached transcription",
    )
    ap.add_argument(
        "--refresh-ai-cache",
        action="store_true",
        help="re-run both candidate-mark and header/notes AI caches",
    )
    args = ap.parse_args()

    wanted = {int(x) for x in args.pages.split(",") if x.strip()} if args.pages else None
    payload = process_pdf(
        args.pdf,
        wanted=wanted,
        no_ai=args.no_ai,
        no_mark_ai=args.no_mark_ai,
        refresh_ai_cache=args.refresh_ai_cache,
        refresh_header_cache=args.refresh_header_cache,
    )
    Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out} ({payload['page_count']} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
