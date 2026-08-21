"""Resolve one localized physical mark from its crop.

v3.4 policy: PHYSICAL CIRCLES/ARCS ONLY; NEVER GUESS INTENT.

Safety is structural:
  * Python builds the candidate universe from locked catalog coordinates.
  * The model returns integer candidate indexes only.
  * Python alone maps indexes to billing codes.
  * Out-of-range indexes are rejected.

Decision policy:
  * A real handwritten loop/arc must be physically visible.
  * Touching an adjacent row is NOT enough to include it.
  * The reader estimates how much of each printed code box is genuinely
    enclosed/surrounded by the physical mark.
  * Adjacent rows require >= 50% physical coverage to be included.
  * For an incomplete arc, a clearly dominant physical row may still be kept
    even if the visible arc does not form a closed 50% enclosure.
  * Geometry is telemetry only. It never adjudicates ownership.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

MIN_PHYSICAL_COVERAGE = 0.50

SYSTEM = r"""You inspect ONE cropped region of a medical superbill. Report ONLY real, physically visible handwritten circle/oval/arc evidence.

DO NOT infer what the writer probably intended. DO NOT complete a missing curve in your imagination. DO NOT treat printed lines, subtraction residue, registration halos, ticks, arrows, underlines, signatures, strike-throughs, or stray pen fragments as circles.

For EACH real handwritten circle-like mark you can physically see:
1. Decide whether it is COMPLETE or INCOMPLETE.
2. For each supplied row that is actually affected, estimate physical_coverage from 0.00 to 1.00. This means the fraction of that row's PRINTED CODE BOX that is visibly enclosed/surrounded by the handwritten loop/arc envelope.
3. A stroke merely touching, grazing, or crossing the boundary of an adjacent row is NOT meaningful coverage. Do NOT include that adjacent row unless at least about 50% of its printed code box is genuinely inside/surrounded by the physical mark.
4. COMPLETE loop: include every row with >= 0.50 physical coverage. Do not add neighboring rows for slight overlap.
5. INCOMPLETE arc: if the visible arc itself clearly has one dominant physical row, set dominant_index to that row. Otherwise dominant_index must be null. Still report physical_coverage for every genuinely affected row.
6. Multiple physical marks are allowed. Report each separately.

IMPORTANT EXAMPLES:
- A circle centered around row B whose top stroke barely enters row A: row A coverage is low (<0.50), so report only row B.
- One large circle genuinely spanning two code boxes with most of both boxes inside: report both rows, each coverage >=0.50.
- A broken but clearly real arc around one row: report that row and set it dominant when the visible arc clearly favors it.
- Printed residue or a faint leftover line that is not clearly deliberate handwriting: report no circle mark.

CRITICAL NON-HALLUCINATION RULE:
- If you cannot see a real handwritten circle/arc, return no circle_marks.
- Never infer a mark from a geometry hint or from where a circle 'should' be.
- Do not extrapolate a faint leftover arc into a circle unless the handwriting itself is visibly real enough to identify as a deliberate circle-like mark.

SAFETY RULE:
- The supplied candidate list is the complete allowed universe.
- Return ONLY integer indexes from that list.
- Never output a billing code as an answer. Never invent a row.

Return ONLY JSON, no prose and no markdown fence:
{
  "circle_marks": [
    {
      "completeness": "complete|incomplete",
      "rows": [
        {"index": <int>, "physical_coverage": <0.00..1.00>}
      ],
      "dominant_index": <int or null>,
      "confidence": <0..1>,
      "why": "<max 22 words describing only visible physical stroke>"
    }
  ],
  "other_marks": ["<short description>", ...]
}
"""


def build_user_block(mark, png: bytes, retry_note: str | None = None) -> list[dict]:
    rows = []
    for i, c in enumerate(mark.candidates):
        rows.append({
            "index": i,
            "printed_code": c["code"],
            "printed_label": c["description"],
            "position": f"row {i + 1} of {len(mark.candidates)} from the top",
        })
    text = (
        "Rows visible in this crop, top to bottom:\n"
        + json.dumps(rows, indent=1)
        + "\n\nFind only real handwritten circles/arcs. For every affected row, return physical_coverage. "
          "A neighboring row must not be included for a slight touch; it needs at least 0.50 coverage unless it is the clear dominant row of a real incomplete arc."
    )
    if retry_note:
        text += "\n\n" + retry_note
    return [
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii")}},
        {"type": "text", "text": text},
    ]


def _parse(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model response")
    data = json.loads(t[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("model response is not a JSON object")
    return data


def _call_reader(client, model: str, mark, png: bytes, max_tokens: int,
                 retry_note: str | None = None) -> str:
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_user_block(mark, png, retry_note)}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def adjudicate(client, model: str, mark, png: bytes, max_tokens: int = 900) -> dict:
    """Return a validated physical-mark decision for one crop.

    A malformed model response is retried once. v3.3 dropped the entire mark on
    a single JSON-format failure, which caused clear J-code circles to vanish.
    """
    if not mark.candidates:
        return {
            "status": "no_candidate_rows",
            "circle_marks": [],
            "other_marks": [],
            "invalid_indexes_rejected": [],
            "raw": None,
            "reader_attempts": 0,
        }

    text = ""
    data = None
    parse_error = None
    attempts = 0
    for attempt in range(2):
        attempts += 1
        retry_note = None if attempt == 0 else (
            "Your previous response was not valid for the required JSON schema. "
            "Return ONLY the exact JSON object requested. Use circle_marks[].rows[] with integer index and numeric physical_coverage."
        )
        try:
            text = _call_reader(client, model, mark, png, max_tokens, retry_note)
            data = _parse(text)
            # If a circle was reported but the required rows/coverage schema is
            # missing, treat that as a schema failure and retry once.
            malformed_group = any(
                not isinstance(g, dict) or not isinstance(g.get("rows"), list)
                for g in (data.get("circle_marks") or [])
            )
            if malformed_group:
                raise ValueError("circle_marks group missing required rows[] coverage schema")
            parse_error = None
            break
        except Exception as exc:
            parse_error = exc
            data = None

    if data is None:
        return {
            "status": "unparseable",
            "error": str(parse_error),
            "circle_marks": [],
            "other_marks": [],
            "invalid_indexes_rejected": [],
            "raw": text,
            "reader_attempts": attempts,
        }

    n = len(mark.candidates)
    rejected: list[Any] = []

    def resolve_index(idx):
        if not isinstance(idx, int) or idx < 0 or idx >= n:
            rejected.append(idx)
            return None
        return mark.candidates[idx]

    circle_marks = []
    for group in data.get("circle_marks") or []:
        completeness = str(group.get("completeness") or "").strip().lower()
        if completeness not in {"complete", "incomplete"}:
            continue

        members = []
        valid_indexes = []
        seen = set()
        for row in group.get("rows") or []:
            if not isinstance(row, dict):
                continue
            idx = row.get("index")
            c = resolve_index(idx)
            if c is None or c["code"] in seen:
                continue
            try:
                coverage = float(row.get("physical_coverage"))
            except (TypeError, ValueError):
                continue
            coverage = max(0.0, min(1.0, coverage))
            seen.add(c["code"])
            valid_indexes.append(idx)
            members.append({
                "code": c["code"],
                "description": c["description"],
                "section": c["section"],
                "kind": c.get("kind", "procedure"),
                "candidate_index": idx,
                "physical_coverage": round(coverage, 3),
            })
        if not members:
            continue

        dominant_index = group.get("dominant_index")
        if dominant_index is not None:
            if not isinstance(dominant_index, int) or dominant_index not in valid_indexes:
                rejected.append(dominant_index)
                dominant_index = None

        circle_marks.append({
            "members": members,
            "completeness": completeness,
            "dominant_index": dominant_index,
            "confidence": float(group.get("confidence", 0.0) or 0.0),
            "why": str(group.get("why", ""))[:180],
            "evidence_type": "physical_circle_or_arc",
        })

    return {
        "status": "ok_after_retry" if attempts > 1 else "ok",
        "circle_marks": circle_marks,
        "other_marks": [str(x)[:120] for x in (data.get("other_marks") or [])][:8],
        "invalid_indexes_rejected": rejected,
        "candidate_count": n,
        "reader_attempts": attempts,
    }


def reconcile(mark, decision: dict, promote_confidence: float,
              hint_margin: float | None = None,
              min_physical_coverage: float = MIN_PHYSICAL_COVERAGE) -> dict:
    """Apply visible physical circle/arc evidence to locked candidates.

    v3.4 contract:
      * No real physical circle/arc -> no code.
      * Adjacent-row contact alone is not enough.
      * Rows with >= min_physical_coverage are confirmed.
      * For a real incomplete arc, a declared visually dominant row is retained
        even if the arc does not form a >=50% enclosure; adjacent rows still
        require >=50% coverage.
      * Geometry is telemetry only and never adjudicates.
    """
    hint = mark.hint or []
    top = hint[0] if hint else None

    def hint_meta(code: str) -> dict:
        rank = next((i for i, h in enumerate(hint) if h["code"] == code), None)
        score = next((h["score"] for h in hint if h["code"] == code), None)
        return {
            "geometry_hint_rank": rank,
            "geometry_hint_score": score,
            "geometry_hint_agrees": bool(top and top.get("code") == code),
        }

    confirmed: list[dict] = []

    for mark_no, group in enumerate(decision.get("circle_marks", []), start=1):
        members = list(group.get("members") or [])
        if not members:
            continue
        completeness = group.get("completeness")
        dominant_index = group.get("dominant_index")

        selected_by_index: dict[int, dict] = {
            int(m["candidate_index"]): m
            for m in members
            if float(m.get("physical_coverage", 0.0) or 0.0) >= float(min_physical_coverage)
        }

        # A real incomplete arc may be visibly centered around one row without
        # enough of a closed envelope to estimate >=50% coverage. Keep that
        # dominant row, but never use dominance to pull in an adjacent row.
        if completeness == "incomplete" and dominant_index is not None:
            dominant = next((m for m in members
                             if m.get("candidate_index") == dominant_index), None)
            if dominant is not None:
                selected_by_index[int(dominant_index)] = dominant

        selected = list(selected_by_index.values())
        if not selected:
            continue

        for member in selected:
            if completeness == "complete":
                resolution = "physical_complete_circle_material_overlap"
                relation = "materially_enclosed_or_overlapped"
            elif dominant_index is not None and member.get("candidate_index") == dominant_index:
                resolution = "physical_incomplete_arc_visual_majority"
                relation = "dominant_visible_arc"
            else:
                resolution = "physical_incomplete_arc_material_overlap"
                relation = "materially_affected_arc"

            entry = dict(member)
            entry.update(hint_meta(member["code"]))
            entry.update({
                "confidence": group.get("confidence", 0.0),
                "why": group.get("why", ""),
                "status": "confirmed",
                "resolution": resolution,
                "physical_relation": relation,
                "physical_mark_id": mark_no,
                "circle_completeness": completeness,
                "physical_coverage_threshold": float(min_physical_coverage),
            })
            confirmed.append(entry)

    confirmed_by_code: dict[str, dict] = {}
    for item in confirmed:
        prev = confirmed_by_code.get(item["code"])
        if prev is None or item.get("confidence", 0) > prev.get("confidence", 0):
            confirmed_by_code[item["code"]] = item

    confirmed = list(confirmed_by_code.values())
    return {
        "mark_id": mark.id,
        "bbox": mark.bbox,
        "stroke_px": mark.stroke_px,
        "max_clearance_px": mark.max_clearance,
        "confirmed": confirmed,
        "manual_review": [],
        "other_marks": decision.get("other_marks", []),
        "physical_circle_marks_visible": len(decision.get("circle_marks", [])),
        "geometry_hint": hint[:4],
        "geometry_policy": "diagnostic_only_never_adjudicates",
        "physical_coverage_threshold": float(min_physical_coverage),
        "reader_status": decision.get("status"),
        "reader_attempts": decision.get("reader_attempts", 0),
        "invalid_indexes_rejected": decision.get("invalid_indexes_rejected", []),
        "no_selection": not confirmed,
    }
