"""Handwriting isolation for the locked superbill template.

The v2 residual layer subtracted the aligned reference and then scored every
locked code row for ring-shaped evidence.  Two failure modes came out of that:

  * registration halo (a 1-2px sliver hugging a printed stroke) scored like a
    partial arc on blank cells -> Robert/Hope false positives;
  * one physical loop produced comparable scores on two adjacent rows -> the
    Ramiro ambiguity.

This module fixes the first one, which the issue history correctly identifies
as the upstream defect.  The gate is a distance transform, not a threshold on
arc quality:

    A misregistration halo lives entirely within ~1-2px of printed ink.
    A real pen stroke has a core several pixels clear of any printed ink.

So we keep a residual connected component only if its *maximum clearance* from
the printed layer exceeds `clearance_min`.  That is a physical property of the
mark, not a tuned score, and it does not care whether the arc is complete.

Nothing here decides which billing code was selected.  This module only answers
"which pixels on this page are handwriting?".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

DEFAULTS: dict[str, Any] = {
    # Adaptive binarisation. Block/C are shared by page and reference so the two
    # ink masks are produced by an identical operator.
    "bin_block": 41,
    "bin_C": 12,

    # How far the printed mask is grown before subtraction. Small: we rely on
    # the clearance gate, not on erasing generously.
    "printed_dilate": 3,

    # THE gate. Components whose max distance from printed ink never reaches
    # this are registration/scan halo. 2.5px at 200 DPI ~= 0.3mm.
    "clearance_min": 2.5,

    # Ignore specks. Deliberately low; the clearance gate does the real work.
    "min_component_area": 40,
}


@dataclass
class HandwritingLayer:
    strokes: np.ndarray          # uint8 0/1 — handwriting only
    printed: np.ndarray          # uint8 0/1 — reference ink
    page_ink: np.ndarray         # uint8 0/1 — all ink on the aligned page
    clearance: np.ndarray        # float32 — px distance from nearest printed ink
    stats: dict = field(default_factory=dict)


def ink_mask(gray: np.ndarray, block: int, C: int) -> np.ndarray:
    return (
        cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, int(block), int(C),
        ) > 0
    ).astype(np.uint8)


def isolate(page_gray: np.ndarray, ref_gray: np.ndarray,
            params: dict | None = None) -> HandwritingLayer:
    p = dict(DEFAULTS)
    p.update(params or {})

    printed = ink_mask(ref_gray, p["bin_block"], p["bin_C"])
    page = ink_mask(page_gray, p["bin_block"], p["bin_C"])

    k = int(p["printed_dilate"])
    printed_grown = cv2.dilate(printed, np.ones((k, k), np.uint8), iterations=1)
    residual = (page & (1 - printed_grown)).astype(np.uint8)

    # Distance from every pixel to the nearest printed pixel.
    clearance = cv2.distanceTransform((1 - printed).astype(np.uint8), cv2.DIST_L2, 3)

    n, lab, cc, _ = cv2.connectedComponentsWithStats(residual, 8)
    strokes = np.zeros_like(residual)
    kept = dropped_halo = dropped_small = 0
    halo_px = 0
    for i in range(1, n):
        area = int(cc[i, cv2.CC_STAT_AREA])
        sel = lab == i
        if area < int(p["min_component_area"]):
            dropped_small += 1
            continue
        if float(clearance[sel].max()) < float(p["clearance_min"]):
            dropped_halo += 1
            halo_px += area
            continue
        strokes[sel] = 1
        kept += 1

    return HandwritingLayer(
        strokes=strokes,
        printed=printed,
        page_ink=page,
        clearance=clearance,
        stats={
            "residual_px": int(residual.sum()),
            "handwriting_px": int(strokes.sum()),
            "components_kept": kept,
            "components_dropped_as_halo": dropped_halo,
            "components_dropped_as_speck": dropped_small,
            "halo_px_removed": int(halo_px),
            "clearance_min": float(p["clearance_min"]),
            "printed_dilate": int(p["printed_dilate"]),
        },
    )


def glyph_boxes(ref_gray: np.ndarray, cells: list[dict],
                params: dict | None = None, trim: int = 12) -> dict[str, tuple]:
    """Tight bounding box of the *printed code text* inside each locked cell.

    The catalog bbox is the whole table cell, so its edges are the printed rules.
    Bracket geometry ("is there pen ink to the left AND right of the digits?")
    only works against the glyph extent, so we precompute it once per template.
    """
    p = dict(DEFAULTS)
    p.update(params or {})
    printed = ink_mask(ref_gray, p["bin_block"], p["bin_C"]).astype(bool)
    out: dict[str, tuple] = {}
    for c in cells:
        x1, y1, x2, y2 = c["bbox"]
        sub = printed[y1 + 5:y2 - 5, x1 + trim:x2 - trim]
        ys, xs = np.nonzero(sub)
        if len(xs) < 20:
            continue
        out[c["code"]] = (
            x1 + trim + int(xs.min()), y1 + 5 + int(ys.min()),
            x1 + trim + int(xs.max()), y1 + 5 + int(ys.max()),
        )
    return out
