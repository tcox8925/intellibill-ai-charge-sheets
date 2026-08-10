"""Geometry-only locked-template verification. Makes no AI or database calls."""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from render import render_pdf
from settings import get_settings
from template_registry import locked_template, load_manifest_and_catalog


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="")
    args = ap.parse_args()
    wanted = {int(x) for x in args.pages.split(",") if x.strip()} if args.pages else None

    s = get_settings()
    load_manifest_and_catalog()
    template = locked_template()
    rendered = render_pdf(Path(args.pdf).read_bytes(), s.render_dpi, wanted=wanted)
    mismatches = 0

    for rp in rendered:
        bgr = cv2.imdecode(np.frombuffer(rp.png_bytes, np.uint8), cv2.IMREAD_COLOR)
        a = template.align(bgr)
        if not template.is_match(a):
            mismatches += 1
            print(f"page {rp.page_number:>3}: match={a.match_score:.3f} TEMPLATE_MISMATCH")
            continue
        proc, dx, geom = template.detect(a)
        print(
            f"page {rp.page_number:>3}: match={a.match_score:.3f} "
            f"proc={[x['code'] for x in proc]} dx={[x['code'] for x in dx]} "
            f"black_enabled={geom.get('residual_geometry_enabled')}"
        )

    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
