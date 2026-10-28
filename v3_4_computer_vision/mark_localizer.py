"""Locate discrete handwritten marks and prepare them for adjudication.

Architectural change from v2.x: this is *mark-centric*, not row-centric.

v2.x asked, for each of 206 locked rows: "does a ring surround me?"  One
physical loop therefore produced evidence on several rows and the pipeline had
to invent tie-break rules to decide ownership, while blank rows could
accumulate evidence out of nothing.

v3 asks: "where are the physical marks on this page?" and only then "which
locked rows does this one mark plausibly refer to?"  Two consequences:

  * a page with three separate circles yields three marks with independent
    candidate sets, so the Richard multi-circle case needs no special rule;
  * a blank region yields no mark at all, so it can never produce a code.

The localizer is deliberately *high recall / low precision*.  It is a finder,
not a classifier.  Its output is a small number of crops, each of which is
resolved by looking at the actual image.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

DEFAULTS: dict[str, Any] = {
    # Bridge pen lifts AND the gaps torn in a loop where it crosses printed
    # rules/text and was removed by print subtraction. Measured gaps on real
    # sheets run 5-16px at 200 DPI.
    "bridge_kernel": 15,

    # A mark must be this much ink and this wide/tall to be worth a look.
    "min_stroke_px": 80,
    "min_extent_px": 30,

    # Active table band on the locked template (y). Header/footer handwriting is
    # captured separately and never contributes billing codes.
    "content_y_min": 150,
    "content_y_max": 1585,

    # Declared write-in blocks on this locked template, in reference pixels:
    # the patient header, the insurance/copay/payment block, and the bottom log
    # strip. Handwriting whose centroid lands here is cursive, never a loop
    # around a code, so it is classified write_in and can never reach the code
    # path. Column A's last code ends at y=1345 and a legitimate circle around a
    # column B code reaches no further left than x~396, so the insurance box is
    # bounded at x<390 to keep low column B rows reachable.
    # RECALIBRATE THIS WITH THE TEMPLATE — it is locked-template geometry.
    "write_in_regions": [
        {"name": "patient_header", "bbox": [0, 0, 2122, 150]},
        {"name": "insurance_payment_block", "bbox": [0, 1350, 390, 1649]},
        {"name": "bottom_log_strip", "bbox": [0, 1600, 2122, 1649]},
    ],
    # Sanity flag only: a real loop around one code is small. Anything far above
    # this inside the table band is almost certainly cursive that escaped the
    # declared regions, and is flagged for calibration rather than silently cut.
    "cursive_stroke_px_hint": 2500,

    # How far around a mark we look for locked rows it might refer to.
    "candidate_pad_x": 30,
    "candidate_pad_y": 18,
    "max_candidates": 24,

    # Crop geometry handed to the visual reader.
    "crop_pad_x": 70,
    "crop_pad_y": 42,
    "crop_extra_right": 340,     # reach the description column
    "crop_scale": 2.5,

    # Merge two candidates whose crops substantially overlap: a loop severed by
    # a printed rule must not be adjudicated twice.
    "merge_iou": 0.20,

    # Corroborating geometry hint weights (evidence only, never a decision).
    "hint_vertical_weight": 0.5,
    "hint_bracket_weight": 0.5,
    "hint_bracket_px": 20,
    "hint_bracket_reach": 70,
}


@dataclass
class Mark:
    id: int
    bbox: list[int]                       # x1,y1,x2,y2 of the stroke group
    crop_bbox: list[int]
    stroke_px: int
    max_clearance: float
    band: str                             # table | write_in | header | footer
    region: str | None = None             # declared write-in region, if any
    oversized: bool = False               # cursive-sized blob inside the table band
    candidates: list[dict] = field(default_factory=list)   # locked rows, top->bottom
    hint: list[dict] = field(default_factory=list)         # geometry corroboration
    mask: np.ndarray | None = None


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / max(1.0, ua)


def _geometry_hint(mask: np.ndarray, bbox: list[int], cells: list[dict],
                   glyphs: dict, p: dict) -> list[dict]:
    """Deterministic corroboration for ownership. NOT the decision.

    Two features that survive print subtraction, unlike ring completeness:
      vertical  — the stroke's median y against the row band centre;
      bracket   — pen ink on BOTH sides of the printed digits within the row
                  band, which is the left/right signature of an ellipse and is
                  drawn in white space so subtraction never erases it.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return []
    median_y = float(np.median(ys))
    out = []
    for c in cells:
        g = glyphs.get(c["code"])
        if not g:
            continue
        gx1, gy1, gx2, gy2 = g
        x1, y1, x2, y2 = c["bbox"]
        if (gx2 < bbox[0] - 60 or gx1 > bbox[2] + 60
                or gy2 < bbox[1] - 30 or gy1 > bbox[3] + 30):
            continue
        cy, ph = (y1 + y2) / 2.0, float(y2 - y1)
        vertical = float(np.exp(-((median_y - cy) / (ph * 0.9)) ** 2))
        band = (ys >= y1 - ph * 0.6) & (ys <= y2 + ph * 0.6)
        reach = float(p["hint_bracket_reach"])
        need = float(p["hint_bracket_px"])
        left = int(((xs < gx1) & (xs > gx1 - reach) & band).sum())
        right = int(((xs > gx2) & (xs < gx2 + reach) & band).sum())
        bracket = min(1.0, left / need) * min(1.0, right / need)
        score = (float(p["hint_vertical_weight"]) * vertical
                 + float(p["hint_bracket_weight"]) * bracket)
        out.append({
            "code": c["code"], "score": round(score, 4),
            "vertical": round(vertical, 3), "bracket": round(bracket, 3),
            "left_px": left, "right_px": right,
        })
    out.sort(key=lambda d: -d["score"])
    return out[: int(p["max_candidates"])]


def _candidate_rows_for_mark(m: Mark, cells: list[dict], glyphs: dict,
                             p: dict) -> list[dict]:
    """Build a high-recall locked candidate list for one physical mark.

    v3.3 used a top-to-bottom bbox intersection capped at 8 rows. On dense
    regions this could drop a genuinely circled row from the candidate universe
    (for example J3301 after interleaved diagnosis rows), even though the crop
    visibly contained it. v3.4 ranks nearby locked rows by physical proximity
    to observed stroke and keeps a larger bounded set.

    This ranking is candidate-generation only. It never confirms a code.
    """
    px, py = int(p["candidate_pad_x"]), int(p["candidate_pad_y"])
    near = [c for c in cells
            if not (c["bbox"][2] < m.bbox[0] - px or c["bbox"][0] > m.bbox[2] + px
                    or c["bbox"][3] < m.bbox[1] - py or c["bbox"][1] > m.bbox[3] + py)]
    if not near:
        return []

    mask = m.mask
    h, w = mask.shape if mask is not None else (0, 0)
    ranked = []
    for c in near:
        gx1, gy1, gx2, gy2 = glyphs.get(c["code"], c["bbox"])
        # A circle stroke usually lives in white space immediately around the
        # printed code. Count physically observed handwriting near that glyph.
        pad_x, pad_y = 48, 20
        x1 = max(0, int(gx1) - pad_x)
        y1 = max(0, int(gy1) - pad_y)
        x2 = min(w, int(gx2) + pad_x) if w else int(gx2) + pad_x
        y2 = min(h, int(gy2) + pad_y) if h else int(gy2) + pad_y
        stroke_near = int(mask[y1:y2, x1:x2].sum()) if mask is not None and x2 > x1 and y2 > y1 else 0

        cx = (c["bbox"][0] + c["bbox"][2]) / 2.0
        cy = (c["bbox"][1] + c["bbox"][3]) / 2.0
        dx = max(m.bbox[0] - cx, 0.0, cx - m.bbox[2])
        dy = max(m.bbox[1] - cy, 0.0, cy - m.bbox[3])
        dist = (dx * dx + dy * dy) ** 0.5
        ranked.append((c, stroke_near, dist))

    ranked.sort(key=lambda t: (-t[1], t[2], t[0]["bbox"][1], t[0]["bbox"][0]))
    chosen = [t[0] for t in ranked[: int(p["max_candidates"])]]
    chosen.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))
    return chosen


def find_marks(layer, catalog: dict, glyphs: dict,
               params: dict | None = None) -> list[Mark]:
    p = dict(DEFAULTS)
    p.update(params or {})
    hw = layer.strokes
    h, w = hw.shape
    cells = catalog["cells"]

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (int(p["bridge_kernel"]), int(p["bridge_kernel"])))
    glued = cv2.dilate(hw, k, iterations=1)
    n, lab, cc, _ = cv2.connectedComponentsWithStats(glued, 8)

    raw: list[Mark] = []
    for i in range(1, n):
        x, y, cw, ch = (int(cc[i, cv2.CC_STAT_LEFT]), int(cc[i, cv2.CC_STAT_TOP]),
                        int(cc[i, cv2.CC_STAT_WIDTH]), int(cc[i, cv2.CC_STAT_HEIGHT]))
        comp = (lab == i).astype(np.uint8)
        mask = (hw & comp).astype(np.uint8)
        stroke_px = int(mask.sum())
        if stroke_px < int(p["min_stroke_px"]) or max(cw, ch) < int(p["min_extent_px"]):
            continue
        ys, xs = np.nonzero(mask)
        bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        mid_x = (bbox[0] + bbox[2]) / 2.0
        mid_y = (bbox[1] + bbox[3]) / 2.0
        region = next((r["name"] for r in p["write_in_regions"]
                       if r["bbox"][0] <= mid_x <= r["bbox"][2]
                       and r["bbox"][1] <= mid_y <= r["bbox"][3]), None)
        if region:
            band = "write_in"
        elif mid_y < float(p["content_y_min"]):
            band = "header"
        elif mid_y > float(p["content_y_max"]):
            band = "footer"
        else:
            band = "table"
        crop = [max(0, bbox[0] - int(p["crop_pad_x"])),
                max(0, bbox[1] - int(p["crop_pad_y"])),
                min(w, bbox[2] + int(p["crop_pad_x"]) + int(p["crop_extra_right"])),
                min(h, bbox[3] + int(p["crop_pad_y"]))]
        m = Mark(
            id=len(raw), bbox=bbox, crop_bbox=crop, stroke_px=stroke_px,
            max_clearance=round(float(layer.clearance[mask.astype(bool)].max()), 2),
            band=band, mask=mask,
        )
        m.region = region
        m.oversized = bool(band == "table"
                           and stroke_px > int(p["cursive_stroke_px_hint"]))
        raw.append(m)

    # Merge fragments of the same physical mark (loop severed by a printed rule).
    merged: list[Mark] = []
    for m in sorted(raw, key=lambda z: -z.stroke_px):
        hit = None
        for t in merged:
            if t.band == m.band and _iou(t.crop_bbox, m.crop_bbox) >= float(p["merge_iou"]):
                hit = t
                break
        if hit is None:
            merged.append(m)
            continue
        hit.bbox = [min(hit.bbox[0], m.bbox[0]), min(hit.bbox[1], m.bbox[1]),
                    max(hit.bbox[2], m.bbox[2]), max(hit.bbox[3], m.bbox[3])]
        hit.crop_bbox = [min(hit.crop_bbox[0], m.crop_bbox[0]),
                         min(hit.crop_bbox[1], m.crop_bbox[1]),
                         max(hit.crop_bbox[2], m.crop_bbox[2]),
                         max(hit.crop_bbox[3], m.crop_bbox[3])]
        hit.stroke_px += m.stroke_px
        hit.max_clearance = max(hit.max_clearance, m.max_clearance)
        hit.mask = ((hit.mask | m.mask) > 0).astype(np.uint8)

    marks: list[Mark] = []
    for idx, m in enumerate(sorted(merged, key=lambda z: (z.bbox[1], z.bbox[0]))):
        m.id = idx
        if m.band == "table":
            m.candidates = _candidate_rows_for_mark(m, cells, glyphs, p)
            m.hint = _geometry_hint(m.mask, m.bbox, m.candidates, glyphs, p)
        marks.append(m)
    return marks


def render_crop(page_gray: np.ndarray, mark: Mark, scale: float | None = None) -> bytes:
    """PNG bytes of the mark's neighbourhood, upscaled for legibility.

    The crop is the *unmodified* aligned page — the reader must see the printed
    code text and the pen stroke together, exactly as a human biller would.
    """
    s = float(scale if scale is not None else DEFAULTS["crop_scale"])
    x1, y1, x2, y2 = mark.crop_bbox
    sub = page_gray[y1:y2, x1:x2]
    if sub.size == 0:
        return b""
    big = cv2.resize(sub, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".png", big)
    return buf.tobytes() if ok else b""
