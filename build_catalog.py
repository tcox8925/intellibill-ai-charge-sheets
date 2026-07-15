"""
build_catalog.py — create a template catalog from a sample page.

This is the "new template" step of the pipeline. run.py calls it automatically
when a page's fingerprint matches no known catalog: it does one Opus vision pass
over the printed grid, writes catalog_<template_id>.json, and uses it right away.
A rescan of a form already in the registry matches on content anchors and reuses
the existing catalog — it does NOT rebuild, and does not go to review.

You can also run it by hand to (re)build a catalog for a form:
    python build_catalog.py <pdf_or_image> [--page 1] [--out catalog.json]
                            [--template-id nwa_internal_medicine_superbill]

Low-confidence cells are marked needs_verify as an informational hint; the
catalog is directly usable. If a build ever comes out wrong, rescan/rebuild.
Auth is shared with run.py (EOB KV -> Foundry, or env fallback).
"""

import argparse, base64, io, json, os, subprocess, glob, re, tempfile
from PIL import Image

from extract import MODEL          # shared Opus model string

# Section headers we expect on this family of superbills — also used as the
# fingerprint's label anchors. Editable if a new form drops/renames sections.
EXPECTED_SECTIONS = [
    "Therapeutic Injections", "Labs", "Office Services", "Cardiovascular",
    "CNS", "Dermatology", "Endocrine", "Gastroenterology",
    "Genital Urinary System", "Musculoskeletal", "Pulmonary", "Radiology",
    "Hematology", "Miscellaneous",
]

VERIFY_THRESHOLD = 0.75   # cells read below this get needs_verify=true

SYSTEM = (
    "You transcribe the PRINTED grid of a blank medical superbill into strict "
    "JSON. No prose, no markdown. Read ONLY the pre-printed cells (ignore any "
    "handwriting, circles, or notes). Every code cell has a code and a "
    "description; report them verbatim, do not correct spelling. Classify each "
    "code as CPT (5-digit or letter+4, e.g. 99214, J1885, G0477) or ICD10 "
    "(letter + digits with a dot, e.g. M54.5, I10, R73.01). Give a per-cell "
    "confidence in [0,1]; lower it when the print is small, broken, or unclear."
)

def build_prompt(sections: list[str]) -> str:
    schema = {
        "sections": [
            {"section": "one of the known section headers",
             "code_type": "CPT|ICD10|MIXED",
             "cells": [{"code": "", "description": "",
                        "code_type": "CPT|ICD10 (only if section is MIXED)",
                        "confidence": 0.0}]}
        ]
    }
    return (
        "Transcribe every printed code cell on this superbill, grouped by its "
        "section header. Known section headers (use these exact names):\n"
        f"{', '.join(sections)}\n\n"
        "Return JSON exactly like:\n"
        f"{json.dumps(schema, indent=2)}\n\n"
        "Be exhaustive — include EVERY row in every section. If a code is "
        "partially illegible, give your best reading and set confidence < 0.5."
    )

def load_image_b64(path: str, page: int, dpi: int = 300, rotate: int = 90,
                   max_px: int = 2600) -> str:
    # render one page to PNG (high DPI: catalog accuracy is worth it, it's 1x)
    if path.lower().endswith(".pdf"):
        d = tempfile.mkdtemp()
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page),
                        "-l", str(page), path, os.path.join(d, "p")], check=True)
        path = glob.glob(os.path.join(d, "p-*.png"))[0]
    im = Image.open(path)
    if rotate:
        im = im.rotate(rotate, expand=True)
    if max(im.size) > max_px:
        s = max_px / max(im.size)
        im = im.resize((int(im.width * s), int(im.height * s)))
    buf = io.BytesIO(); im.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Salvage a truncated response: scan tracking string/bracket state,
        # remember the last position a value fully closed and the open stack
        # there, then cut to it and append the matching closers.
        stack, instr, esc = [], False, False
        last_idx, last_stack = -1, None
        for i, ch in enumerate(text):
            if esc:
                esc = False; continue
            if ch == "\\":
                esc = True; continue
            if ch == '"':
                instr = not instr; continue
            if instr:
                continue
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
                last_idx, last_stack = i, list(stack)
        if last_idx == -1:
            raise
        closer = {"{": "}", "[": "]"}
        tail = "".join(closer[c] for c in reversed(last_stack))
        return json.loads(text[:last_idx + 1] + tail)

def build_catalog_from_b64(img_b64: str, client, template_id: str,
                           source_label: str = "") -> dict:
    """Core builder: one Opus vision pass over a printed grid -> catalog dict.
    ~200 cells is large, so we give a big token budget, ask for minified JSON,
    and retry once if the response comes back truncated/invalid."""
    prompt = build_prompt(EXPECTED_SECTIONS) + \
        "\n\nOutput MINIFIED JSON on a single line (no newlines, no extra spaces)."
    raw, last_err = None, None
    for attempt in range(2):
        msg = client.messages.create(
            model=MODEL, max_tokens=16000, system=SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt}]}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            raw = _parse_json(text)
            break
        except Exception as e:
            last_err = e  # likely truncation -> try once more
    if raw is None:
        raise ValueError(f"catalog build returned unparseable JSON: {last_err}")

    sections = []
    for sec in raw.get("sections", []):
        cells = []
        for c in sec.get("cells", []):
            cell = {"code": c["code"], "description": c["description"]}
            if sec.get("code_type") == "MIXED" and c.get("code_type"):
                cell["code_type"] = c["code_type"]
            if float(c.get("confidence", 1.0)) < VERIFY_THRESHOLD:
                cell["needs_verify"] = True
            cells.append(cell)
        sections.append({"section": sec["section"],
                         "code_type": sec.get("code_type", "MIXED"),
                         "cells": cells})

    anchor_codes = [s["cells"][0]["code"] for s in sections
                    if s["cells"] and not s["cells"][0].get("needs_verify")]
    return {
        "template_id": template_id, "version": 1,
        "source": source_label or "auto-built",
        "status": "active",        # directly usable; rebuild if a scan looks off
        "fingerprint": {"header_labels": EXPECTED_SECTIONS,
                        "anchor_codes": anchor_codes[:12]},
        "header_fields": ["date", "name", "dob", "prn", "insurance",
                          "copay", "amount_paid", "payment_type"],
        "sections": sections,
    }


def build_catalog(source: str, client, page: int = 1, out: str = "catalog.json",
                  template_id: str = "nwa_internal_medicine_superbill",
                  dpi: int = 300) -> str:
    """Build from a PDF page or image file, write JSON, return the path."""
    img = load_image_b64(source, page, dpi)
    cat = build_catalog_from_b64(img, client, template_id,
                                 f"auto-built from {os.path.basename(source)} p{page}")
    json.dump(cat, open(out, "w"), indent=2)
    total = sum(len(s["cells"]) for s in cat["sections"])
    low = sum(1 for s in cat["sections"] for c in s["cells"] if c.get("needs_verify"))
    print(f"wrote {out}: {len(cat['sections'])} sections, {total} cells, "
          f"{low} low-confidence cells marked needs_verify (informational).")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="PDF or image of a representative form page")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--out", default="catalog.json")
    ap.add_argument("--template-id", default="nwa_internal_medicine_superbill")
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args()
    from run import make_client       # lazy import avoids run<->build_catalog loop
    build_catalog(args.source, make_client(), args.page, args.out,
                  args.template_id, args.dpi)

if __name__ == "__main__":
    main()
