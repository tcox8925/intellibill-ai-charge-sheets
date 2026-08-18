"""Constrained AI review of deterministic mark candidates.

Safety boundary:
- locked_template.py decides which locked code coordinates have physical ink
  evidence and which ambiguous groups are strong enough for AI review.
- The model NEVER receives authority to invent a CPT/HCPCS/ICD code.  It only
  classifies labeled candidate anchors (A/B/C...) inside small review crops.
- Weak geometry is never promoted by AI; it remains visible as possible evidence.
- If visual ownership is unclear, candidates remain visible in possible_marks.
- Only a high-confidence visual CIRCLE backed by deterministic confirmation-level
  evidence can promote an existing candidate.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
from typing import Any

import cv2
import numpy as np
from PIL import Image

from settings import get_settings

SYSTEM = (
    "You are a conservative visual mark reviewer for a medical charge-sheet image. "
    "This is NOT a medical-coding task. Never output, infer, copy, or discuss CPT, "
    "HCPCS, ICD, diagnosis, procedure, or billing-code text. Each crop has candidate "
    "row anchors labeled A, B, C, etc. You may select ONLY those labels. Judge only "
    "the original handwriting/ink: circle versus scribble/handwriting versus no "
    "deliberate mark. The A/B/C labels and short pointer ticks are synthetic software "
    "overlays and MUST NOT be counted as handwritten marks. A single large or incomplete "
    "circle may cross adjacent rows; do not call that multiple circles unless two distinct "
    "loops/centers are visibly present. When ownership is not visually clear, return ambiguous."
)

_TRANSIENT = (
    "429", "rate limit", "rate_limit", "peer closed", "connection reset",
    "remoteprotocolerror", "timed out", "timeout", "overloaded", "server disconnected",
)


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        text = text[a:b + 1]
    return json.loads(text)


def _image_b64(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _candidate_cell_by_code(template, code: str):
    for cell in template.cells:
        if str(cell.get("code")) == str(code):
            return cell
    return None


def _confirm_threshold(template, candidate: dict) -> float:
    """Source-specific deterministic score required before AI may promote."""
    source = str(candidate.get("source") or "")
    key = "color_confirm_min" if source == "color_geometry" else "black_confirm_min"
    return float(template.thresholds[key])


def _candidate_has_confirmation_level_evidence(template, candidate: dict) -> bool:
    try:
        score = float(candidate.get("score") or 0.0)
    except Exception:
        score = 0.0
    return score >= _confirm_threshold(template, candidate)


def _mark_is_ai_reviewable(mark: dict, template) -> bool:
    """AI is only useful where geometry already found meaningful evidence.

    Weak residuals are intentionally *not* sent to the model. They remain visible in
    possible_marks, which protects recall without letting the model manufacture a
    confident circle from table/text noise.
    """
    candidates = [c for c in (mark.get("candidate_codes") or []) if c.get("code")]
    return bool(candidates) and any(
        _candidate_has_confirmation_level_evidence(template, c) for c in candidates
    )


def _prepare_regions(aligned_bgr: np.ndarray, possible_marks: list[dict], template):
    """Create a vertically stacked contact sheet with only candidate review crops.

    Candidate labels are overlaid at their locked row centers.  Printed code text may
    still be visible in the crop, but the model is only allowed to return A/B/C labels.
    """
    regions = []
    panels = []
    target_w = 520

    for idx, mark in enumerate(possible_marks, start=1):
        candidates = [c for c in (mark.get("candidate_codes") or []) if c.get("code")]
        rr = mark.get("review_region") or {}
        if (
            not candidates
            or not _mark_is_ai_reviewable(mark, template)
            or not all(k in rr for k in ("x1", "y1", "x2", "y2"))
        ):
            continue

        x1, y1, x2, y2 = (int(rr[k]) for k in ("x1", "y1", "x2", "y2"))
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(aligned_bgr.shape[1], x2), min(aligned_bgr.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            continue

        crop = aligned_bgr[y1:y2, x1:x2].copy()
        # Add a white header so region ids are unambiguous in the collage.
        header_h = 34
        panel = np.full((crop.shape[0] + header_h, crop.shape[1], 3), 255, np.uint8)
        panel[header_h:] = crop
        region_id = f"R{idx}"
        cv2.putText(panel, region_id, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 2, cv2.LINE_AA)

        label_map = {}
        for ci, candidate in enumerate(candidates):
            label = chr(ord("A") + ci)
            code = str(candidate.get("code"))
            cell = _candidate_cell_by_code(template, code)
            label_map[label] = {
                "code": code,
                "score": float(candidate.get("score") or 0.0),
                "source": str(candidate.get("source") or ""),
                "kind": str((cell or {}).get("kind") or mark.get("kind") or ""),
            }
            if cell is not None:
                cx, cy = int(cell["center"][0]) - x1, int(cell["center"][1]) - y1 + header_h
                cy = min(max(header_h + 8, cy), panel.shape[0] - 8)
                # Synthetic anchor pointer. Deliberately avoid circles/ellipses here:
                # a circular overlay can itself be mistaken for a handwritten selection.
                cv2.putText(panel, f"{label}>", (5, cy + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2, cv2.LINE_AA)
                cv2.line(panel, (38, cy), (min(panel.shape[1]-1, 74), cy), (0, 0, 0), 1)

        # Normalize width for a compact one-call collage.
        if panel.shape[1] != target_w:
            scale = target_w / panel.shape[1]
            panel = cv2.resize(panel, (target_w, max(80, int(round(panel.shape[0] * scale)))), interpolation=cv2.INTER_AREA)
        panels.append(panel)
        regions.append({"id": region_id, "mark_index": idx - 1, "labels": label_map})

    if not panels:
        return None, []
    spacer = np.full((10, target_w, 3), 235, np.uint8)
    collage_parts = []
    for i, panel in enumerate(panels):
        if i:
            collage_parts.append(spacer)
        collage_parts.append(panel)
    return np.vstack(collage_parts), regions


def _prompt(regions: list[dict]) -> str:
    spec = []
    for r in regions:
        spec.append({"id": r["id"], "allowed_labels": list(r["labels"].keys())})
    schema = {
        "regions": [{
            "id": "R1",
            "classification": "circle_single|circle_multiple|scribble|ambiguous|none",
            "selected_labels": ["A"],
            "confidence": 0.0,
            "reason": "short visual reason only; no billing-code text",
        }]
    }
    return (
        "Review every crop in the image. Candidate anchors are labeled A/B/C next to their locked row centers.\n"
        f"REGIONS AND ALLOWED LABELS: {json.dumps(spec, separators=(',', ':'))}\n\n"
        "Return STRICT JSON only with this exact top-level shape:\n"
        f"{json.dumps(schema, separators=(',', ':'))}\n\n"
        "Classification rules:\n"
        "- circle_single: one deliberate hand-drawn circle clearly belongs to exactly one anchor.\n"
        "- circle_multiple: two or more SEPARATE deliberate circles/loops with distinct centers are clearly present; select each anchor.\n"
        "  A single large loop crossing two adjacent rows is NOT circle_multiple. Choose the anchor nearest the loop center only when clear; otherwise ambiguous.\n"
        "- scribble: irregular handwriting/overwriting/strike-through exists in the region rather than a clean assignable circle.\n"
        "- ambiguous: real mark evidence exists but ownership between candidate anchors is not clear.\n"
        "- none: no deliberate ORIGINAL handwritten selection/mark is present near these anchors.\n"
        "The A>/B>/C> labels and short pointer ticks are software overlays. Ignore them completely as mark evidence.\n"
        "Printed text, table borders, and row lines are also not mark evidence.\n"
        "For scribble/ambiguous, selected_labels may contain the anchors visibly touched/plausible; otherwise [].\n"
        "Never output any printed code or medical text. Never choose a label not allowed for that region.\n"
        "Be conservative: if you cannot visually prove one anchor owns a circle, use ambiguous rather than guessing."
    )


def review_possible_marks(aligned_bgr: np.ndarray, possible_marks: list[dict], template, client,
                          retries: int = 2) -> dict:
    """Return raw constrained visual classifications keyed by region id."""
    if not possible_marks:
        return {"regions": [], "status": "not_needed"}
    collage, regions = _prepare_regions(aligned_bgr, possible_marks, template)
    if collage is None:
        return {"regions": [], "status": "not_needed"}

    s = get_settings()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _image_b64(collage)}},
        {"type": "text", "text": _prompt(regions)},
    ]
    last_error = None
    for attempt in range(retries + 1):
        try:
            msg = client.messages.create(
                model=s.mark_model,
                max_tokens=max(900, 180 * len(regions)),
                temperature=0,
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            text = "".join(
                getattr(block, "text", "")
                for block in msg.content
                if getattr(block, "type", "") == "text"
            )
            raw = _parse_json(text)
            items = raw.get("regions") if isinstance(raw, dict) else None
            if not isinstance(items, list):
                raise ValueError("mark resolver did not return regions[]")

            by_id = {r["id"]: r for r in regions}
            cleaned = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("id") or "")
                meta = by_id.get(rid)
                if not meta:
                    continue
                allowed = set(meta["labels"])
                classification = str(item.get("classification") or "ambiguous").strip().lower()
                if classification not in {"circle_single", "circle_multiple", "scribble", "ambiguous", "none"}:
                    classification = "ambiguous"
                selected = []
                for label in item.get("selected_labels") or []:
                    label = str(label).strip().upper()
                    if label in allowed and label not in selected:
                        selected.append(label)
                try:
                    confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
                except Exception:
                    confidence = 0.0
                cleaned.append({
                    "id": rid,
                    "mark_index": meta["mark_index"],
                    "classification": classification,
                    "selected_labels": selected,
                    "confidence": round(confidence, 3),
                    "reason": re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:220],
                    "labels": meta["labels"],
                })
            return {"regions": cleaned, "status": "ok"}
        except Exception as exc:
            last_error = exc
            transient = any(p in str(exc).lower() for p in _TRANSIENT)
            if transient and attempt < retries:
                time.sleep(min(8, 2 ** attempt))
                continue
            if attempt < 1:
                continue
            break
    return {"regions": [], "status": "failed", "error": str(last_error)}


def _confirmed_item(template, code: str, label_meta: dict, ai_confidence: float) -> dict | None:
    cell = _candidate_cell_by_code(template, code)
    if cell is None:
        return None
    score = float(label_meta.get("score") or 0.0)
    # Report a conservative blended confidence.  The code itself still came from
    # a locked deterministic candidate; AI contributes only visual ownership.
    confidence = min(0.99, 0.50 + 0.25 * ai_confidence + 0.20 * min(1.0, score))
    return {
        "code": str(cell["code"]),
        "description": str(cell.get("description") or ""),
        "section": str(cell.get("section") or ""),
        "mark": "circle",
        "confidence": round(confidence, 2),
        "detection": str(label_meta.get("source") or "locked_candidate_geometry"),
        "geometry_mode": "ai_verified_candidate",
        "resolution": "constrained_ai_visual_review",
        "ai_resolution_confidence": round(float(ai_confidence), 3),
        "deterministic_candidate_score": round(score, 4),
    }


def apply_mark_review(procedures: list[dict], diagnoses: list[dict], possible_marks: list[dict],
                      review: dict, template) -> tuple[list[dict], list[dict], list[dict], list[dict], dict]:
    """Apply constrained model output without ever allowing a new code.

    Returns: procedures, diagnoses, remaining_possible, suppressed_marks, summary.
    """
    s = get_settings()
    procs = list(procedures)
    dxs = list(diagnoses)
    remaining = [dict(m) for m in possible_marks]
    suppressed = []
    existing = {str(x.get("code")) for x in procs + dxs}
    promoted = []

    # Every possible mark stays displayable even when it is intentionally below
    # the AI review gate. This is the recall-first contract: weak evidence is not
    # silently dropped, but it also cannot be upgraded by the model.
    for mark in remaining:
        raw_candidates = list(mark.get("candidate_codes") or [])
        mark["display_candidate_codes"] = raw_candidates[: max(1, s.mark_display_candidates)]
        if not _mark_is_ai_reviewable(mark, template):
            mark["ai_review"] = {
                "status": "not_reviewed",
                "reason": "geometry_below_ai_review_gate",
                "resolver": s.mark_resolver_version,
                "model": s.mark_model,
            }

    for region in review.get("regions") or []:
        idx = region.get("mark_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(remaining):
            continue
        mark = remaining[idx]
        cls = region.get("classification")
        conf = float(region.get("confidence") or 0.0)
        selected_labels = list(region.get("selected_labels") or [])
        labels = region.get("labels") or {}

        mapped_selected = []
        for label in selected_labels:
            meta = labels.get(label)
            if meta and meta.get("code"):
                mapped_selected.append(str(meta["code"]))

        mark["ai_review"] = {
            "classification": cls,
            "confidence": round(conf, 3),
            "candidate_codes_selected": mapped_selected,
            "reason": region.get("reason") or "",
            "resolver": s.mark_resolver_version,
            "model": s.mark_model,
        }
        # UI/display convenience: never hide raw candidates, but provide the top
        # two most useful choices unless the model narrowed the plausible set.
        raw_candidates = list(mark.get("candidate_codes") or [])
        if mapped_selected:
            chosen = [c for c in raw_candidates if str(c.get("code")) in set(mapped_selected)]
            display = chosen[: max(1, s.mark_display_candidates)]
        else:
            display = raw_candidates[: max(1, s.mark_display_candidates)]
        mark["display_candidate_codes"] = display

        selected_meta = [labels.get(label) or {} for label in selected_labels]
        selected_geometry_eligible = [
            bool(meta.get("code")) and _candidate_has_confirmation_level_evidence(template, meta)
            for meta in selected_meta
        ]
        # Adjacent overlaps outside Office Services are always manual review.
        # In Office Services, AI may resolve exactly one clear circle; ambiguous
        # or multi-circle ownership remains manual review. This preserves the
        # baseline's useful office-code recovery without auto-billing Misc/Labs
        # adjacency.
        is_adjacent_overlap = str(mark.get("reason") or "") == "adjacent_code_overlap"
        selected_sections = []
        for code in mapped_selected:
            cell = _candidate_cell_by_code(template, code)
            if cell is not None:
                selected_sections.append(str(cell.get("section") or ""))
        office_only_selection = bool(selected_sections) and all(
            sec == "Office Services" for sec in selected_sections
        )

        if is_adjacent_overlap:
            can_promote = (
                office_only_selection
                and conf >= s.mark_promote_confidence
                and cls == "circle_single"
                and len(mapped_selected) == 1
                and selected_geometry_eligible == [True]
            )
            if not can_promote:
                mark["manual_review"] = True
                mark["manual_review_reason"] = "adjacent_code_overlap"
        else:
            # Exact v2.2.1 baseline behavior for non-adjacent candidate groups.
            can_promote = (
                conf >= s.mark_promote_confidence
                and (
                    (
                        cls == "circle_single"
                        and len(mapped_selected) == 1
                        and selected_geometry_eligible == [True]
                    )
                    or (
                        cls == "circle_multiple"
                        and len(mapped_selected) >= 2
                        and all(selected_geometry_eligible)
                    )
                )
            )
        mark["ai_review"]["promotion_geometry_eligible"] = bool(can_promote)
        if can_promote:
            for label in selected_labels:
                meta = labels.get(label) or {}
                code = str(meta.get("code") or "")
                if not code or code in existing:
                    continue
                item = _confirmed_item(template, code, meta, conf)
                if item is None:
                    continue
                cell = _candidate_cell_by_code(template, code)
                (procs if cell and cell.get("kind") == "procedure" else dxs).append(item)
                existing.add(code)
                promoted.append(code)
            mark["_remove_from_possible"] = True
            continue

        # High-confidence NONE may de-clutter only WEAK deterministic groups.
        # If any candidate score already reaches its source-specific confirm score,
        # preserve the mark even when AI says none: strong physical evidence must
        # never disappear solely because of a model judgment.
        strong_score_present = any(
            _candidate_has_confirmation_level_evidence(template, raw) for raw in raw_candidates
        )
        if cls == "none" and conf >= s.mark_suppress_confidence and not strong_score_present:
            suppressed_mark = dict(mark)
            suppressed_mark.pop("_remove_from_possible", None)
            suppressed_mark["suppression_reason"] = "ai_high_confidence_no_deliberate_mark_weak_geometry"
            suppressed.append(suppressed_mark)
            mark["_remove_from_possible"] = True
            continue

        if cls == "scribble":
            mark["reason"] = "scribbled_region"
            mark["type"] = "possible_mark"
        elif cls == "ambiguous":
            # Preserve the detector's reason but make the model result explicit.
            mark["ai_ambiguous"] = True

    final_possible = []
    for mark in remaining:
        if mark.pop("_remove_from_possible", False):
            continue
        final_possible.append(mark)

    summary = {
        "status": review.get("status") or "unknown",
        "promoted_codes": promoted,
        "promoted_count": len(promoted),
        "suppressed_count": len(suppressed),
        "remaining_possible_count": len(final_possible),
        "model": s.mark_model,
        "resolver": s.mark_resolver_version,
    }
    return procs, dxs, final_possible, suppressed, summary
