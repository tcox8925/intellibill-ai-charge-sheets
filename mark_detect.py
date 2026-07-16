"""
mark_detect.py — find hand-drawn marks (circles / squiggles) on the superbill,
independent of the LLM, by isolating ink from the printed form.

Dataset trick: every page is the SAME printed grid, so the per-pixel MEDIAN
across many aligned pages reconstructs a blank template (handwriting averages
out). Subtracting that template from a page leaves only that page's ink. On the
residual we find connected components and classify each:

    circle   -> a closed (or nearly closed) loop that encloses area
    squiggle -> an elongated stroke that encloses little (check / underline / scribble)
    blob     -> compact mark (dot / tick)

This gives a geometry-based signal to corroborate or gate the LLM's picks, and
a clean marks-only image to feed the extractor (kills stray-ink false positives
like the 96372 read).

    python mark_detect.py <pdf> [--overlay 8]     # write marks + overlay images
"""

import argparse, os
import numpy as np
import cv2
from scipy import ndimage
from PIL import Image

from run import split_pdf

W, H = 2122, 1649          # common upright frame
INK_THRESH = 55            # residual darkness that counts as ink
MIN_AREA = 250             # hand marks are big; drops text-fragment residue
ROTATE = 90


def _load_gray(path):
    return np.asarray(Image.open(path).rotate(ROTATE, expand=True)
                      .convert("L").resize((W, H)))


def build_template(page_paths):
    """Median of translation-aligned pages ≈ blank printed form."""
    raw = [_load_gray(p).astype(np.float32) for p in page_paths]
    ref = raw[0]
    aligned = []
    for f in raw:
        (dx, dy), _ = cv2.phaseCorrelate(ref, f)
        M = np.float32([[1, 0, -dx], [0, 1, -dy]])
        aligned.append(cv2.warpAffine(f, M, (W, H), borderValue=255))
    return np.median(np.stack(aligned), axis=0), aligned


def isolate_marks(page_gray, template):
    """Binary mask of hand ink: page darker than template, despeckled and only
    lightly bridged so a circle's interior HOLE is preserved for detection."""
    added = np.clip(template - page_gray, 0, 255).astype(np.uint8)
    mask = (added > INK_THRESH).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    return mask


def detect_marks(mask):
    """Contours with RETR_CCOMP: an outer stroke with an inner child contour
    (a hole) is a loop -> circle. Elongated no-hole strokes -> squiggle."""
    cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return []
    hier = hier[0]
    marks = []
    for i, c in enumerate(cnts):
        if hier[i][3] != -1:        # skip child (hole) contours themselves
            continue
        area = cv2.contourArea(c)
        if area < MIN_AREA:
            continue
        x, y, w, h = cv2.boundingRect(c)
        # does this outer contour enclose a hole big enough to be a drawn loop?
        hole = 0.0
        child = hier[i][2]
        while child != -1:
            hole = max(hole, cv2.contourArea(cnts[child]))
            child = hier[child][0]
        bbox_fill = area / float(w * h)
        if hole > 0.20 * (w * h) and min(w, h) > 12:
            kind = "circle"
        elif bbox_fill < 0.35 and max(w, h) > 25:
            kind = "squiggle"
        else:
            kind = "blob"
        marks.append({"kind": kind, "bbox": [int(x), int(y), int(w), int(h)],
                      "center": [int(x + w / 2), int(y + h / 2)], "area": int(area)})
    return marks


def classify_component(comp):
    """circle | squiggle | blob for a component mask cropped to its bbox."""
    stroke = int(comp.sum())
    if stroke == 0:
        return "blob"
    # bridge small gaps so an almost-closed hand circle still reads as a loop
    closed = ndimage.binary_closing(comp, iterations=4)
    filled = ndimage.binary_fill_holes(closed)
    enclosed = int(filled.sum() - closed.sum())
    h, w = comp.shape
    extent = stroke / float(h * w)          # how much of the bbox the stroke fills
    enclosed_ratio = enclosed / float(stroke)
    if enclosed_ratio > 1.0 and min(h, w) > 12:
        return "circle"
    if extent < 0.35 and max(h, w) > 25:
        return "squiggle"
    return "blob"


def detect_marks(mask):
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    marks = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < MIN_AREA:
            continue
        x, y, w, h = (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]),
                      int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]))
        comp = (lab[y:y + h, x:x + w] == i)     # crop to bbox — fast
        kind = classify_component(comp)
        marks.append({"kind": kind, "bbox": [x, y, w, h],
                      "center": [int(cent[i][0]), int(cent[i][1])], "area": area})
    return marks


def overlay(page_gray, marks, out_path):
    im = cv2.cvtColor(page_gray.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    color = {"circle": (0, 170, 0), "squiggle": (0, 140, 255), "blob": (180, 0, 180)}
    for m in marks:
        x, y, w, h = m["bbox"]
        c = color[m["kind"]]
        cv2.rectangle(im, (x, y), (x + w, y + h), c, 2)
        cv2.putText(im, m["kind"], (x, max(0, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 1, cv2.LINE_AA)
    cv2.imwrite(out_path, im)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--overlay", type=int, default=0, help="1-based page to overlay")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()
    pages = split_pdf(args.pdf, "pages", args.dpi)
    template, aligned = build_template(pages)
    Image.fromarray(template.astype(np.uint8)).save("template.png")
    summary = []
    for i, g in enumerate(aligned, 1):
        marks = detect_marks(isolate_marks(g, template))
        counts = {k: sum(1 for m in marks if m["kind"] == k)
                  for k in ("circle", "squiggle", "blob")}
        summary.append((i, counts))
        if i == args.overlay:
            overlay(g, marks, f"marks_overlay_p{i}.png")
            print(f"wrote marks_overlay_p{i}.png")
    for i, c in summary:
        print(f"page {i:>2}: circles={c['circle']} squiggles={c['squiggle']} blobs={c['blob']}")


if __name__ == "__main__":
    main()
