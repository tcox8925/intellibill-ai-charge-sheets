"""Second-pass recall check over the whole page.

The localizer can miss a mark: very faint pencil, a loop drawn almost entirely
on top of printed rules, a mark below the stroke-area floor.  Rather than
loosening the localizer (which reintroduces halo false positives), v3 adds a
cheap independent recall pass.

Safety: this pass returns ONLY normalised coordinates.  It is structurally
incapable of naming a billing code.  Every location it reports is converted to
a synthetic mark, mapped to locked catalog rows by Python, and sent through the
exact same crop adjudication path as a geometric mark.  A code can only ever
enter the result by surviving the crop reader with locked candidates.
"""
from __future__ import annotations

import base64
import json
import re

import cv2
import numpy as np

from mark_localizer import Mark, DEFAULTS as LOC_DEFAULTS, _candidate_rows_for_mark

SYSTEM = """You are checking a scanned medical superbill for hand-drawn circles.

The form is a dense grid of printed billing codes. The provider selects codes by
drawing a loop (circle or ellipse) around them. Loops are often broken where
they cross printed lines.

Find EVERY hand-drawn loop in the image. Ignore: printed text and rules, the
handwritten header at the very top, the handwritten block at the very bottom
left (insurance/copay/payment), strike-throughs, ticks, arrows and stray lines.

Report each loop's centre as a fraction of image width and height.

Return ONLY a JSON object, no prose and no markdown fence:

{"loops": [{"x": <0..1>, "y": <0..1>, "confidence": <0..1>}]}

Do not output any codes, numbers from the form, or text you read on the form.
Coordinates only."""


def _parse(text: str) -> dict:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in audit response")
    return json.loads(t[a:b + 1])


def _panels(gray: np.ndarray, max_width: int, overlap: int):
    h, w = gray.shape
    mid = w // 2
    boxes = [(0, 0, min(w, mid + overlap), h), (max(0, mid - overlap), 0, w, h)]
    out = []
    for (x1, y1, x2, y2) in boxes:
        sub = gray[y1:y2, x1:x2]
        scale = min(1.0, max_width / float(sub.shape[1]))
        img = cv2.resize(sub, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
            if scale < 1.0 else sub
        ok, buf = cv2.imencode(".png", img)
        out.append(((x1, y1, x2, y2), buf.tobytes() if ok else b""))
    return out


def find_missed_marks(client, model: str, page_gray: np.ndarray, catalog: dict,
                      glyphs: dict, existing: list[Mark],
                      max_width: int = 1400, overlap: int = 45,
                      min_separation: int = 55, params: dict | None = None
                      ) -> tuple[list[Mark], dict]:
    p = dict(LOC_DEFAULTS)
    p.update(params or {})
    h, w = page_gray.shape
    known = [((m.bbox[0] + m.bbox[2]) / 2.0, (m.bbox[1] + m.bbox[3]) / 2.0)
             for m in existing]
    reported, errors = [], []

    for (x1, y1, x2, y2), png in _panels(page_gray, max_width, overlap):
        if not png:
            continue
        try:
            msg = client.messages.create(
                model=model, max_tokens=600, system=SYSTEM,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/png",
                                                 "data": base64.b64encode(png).decode("ascii")}},
                    {"type": "text", "text": "List every hand-drawn loop you can see."},
                ]}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            data = _parse(text)
        except Exception as exc:
            errors.append(str(exc)[:160])
            continue
        for loop in (data.get("loops") or [])[:40]:
            try:
                fx, fy = float(loop.get("x")), float(loop.get("y"))
            except (TypeError, ValueError):
                continue
            if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
                continue
            cx = x1 + fx * (x2 - x1)
            cy = y1 + fy * (y2 - y1)
            reported.append((cx, cy, float(loop.get("confidence", 0.5) or 0.5)))

    cells = catalog["cells"]
    recovered: list[Mark] = []
    next_id = (max((m.id for m in existing), default=-1)) + 1
    for cx, cy, conf in reported:
        if cy < p["content_y_min"] or cy > p["content_y_max"]:
            continue
        if any(abs(cx - kx) < min_separation and abs(cy - ky) < min_separation
               for kx, ky in known):
            continue
        if any(abs(cx - m.bbox[0]) < min_separation and abs(cy - m.bbox[1]) < min_separation
               for m in recovered):
            continue
        bbox = [int(max(0, cx - 60)), int(max(0, cy - 22)),
                int(min(w, cx + 60)), int(min(h, cy + 22))]
        crop = [max(0, bbox[0] - p["crop_pad_x"]), max(0, bbox[1] - p["crop_pad_y"]),
                min(w, bbox[2] + p["crop_pad_x"] + p["crop_extra_right"]),
                min(h, bbox[3] + p["crop_pad_y"])]
        m = Mark(id=next_id, bbox=bbox, crop_bbox=crop, stroke_px=0,
                 max_clearance=0.0, band="table",
                 mask=np.zeros(page_gray.shape, np.uint8))
        m.candidates = _candidate_rows_for_mark(m, cells, glyphs, p)
        m.hint = []           # no stroke mask -> no deterministic corroboration
        if not m.candidates:
            continue
        recovered.append(m)
        next_id += 1

    return recovered, {
        "loops_reported": len(reported),
        "recovered_candidates": len(recovered),
        "errors": errors,
    }
