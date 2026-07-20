"""
run.py — orchestrate the charge-sheet pipeline.

  python run.py <pdf> [--pages 8,14] [--catalog catalog.json]

Splits the PDF to page images, runs the vision extractor on each, writes
results.json + run metrics. Page differentiation = one page is one encounter;
each result is keyed by page number and handwritten name/DOB, blanks and
duplicates are flagged.

The per-page loop now lives in process_pdf(), so api.py (the separate FastAPI
service) can reuse the exact same extraction path and, via the on_page hook,
upload each rendered page to blob and persist it to Postgres as it completes
(per-page atomic, like the membercare job model). run.py itself stays local:
no blob, no DB — just results.json.

Needs: pip install anthropic pymupdf pillow   (poppler's pdftoppm also works)
Set ANTHROPIC_API_KEY (prototype only; in-tenant use your KV-backed client).
"""

import argparse, json, os, sys, subprocess, glob, hashlib, datetime
from extract import extract_page, identify_page, load_page_b64, array_b64
from fingerprint import STRONG, score, _norm
from build_catalog import build_catalog

# Raster resolution is FIXED, not configurable: 200 DPI for page extraction
# (a letter page lands ~2200px, matching extract.py's max_px cap) and 300 DPI
# for the one-time catalog build (build_catalog's own default). Raising these
# without also raising extract.max_px is wasted, so they stay baked in.
RENDER_DPI = 200


def load_registry(explicit: str = None) -> list:
    """Load every known catalog (catalog*.json) into a registry. A rescan of a
    known form matches on content anchors and reuses it — no rebuild."""
    reg = {}
    paths = ([explicit] if explicit and os.path.exists(explicit) else []) + \
            sorted(glob.glob("catalog*.json"))
    for p in paths:
        if p in reg:
            continue
        try:
            reg[p] = json.load(open(p))
        except Exception:
            pass
    return list(reg.items())


def pick_catalog(seen_labels, seen_codes, registry):
    """Best content-fingerprint match across known catalogs, or None."""
    best, best_sc = None, -1.0
    for path, cat in registry:
        s = score(seen_labels, seen_codes, cat)
        if s > best_sc:
            best, best_sc = (path, cat), s
    return (best[0], best[1], best_sc) if best else (None, None, 0.0)


def split_pdf(pdf: str, out_dir: str, dpi: int) -> list:
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run(["pdftoppm", "-png", "-r", str(dpi), pdf,
                    os.path.join(out_dir, "page")], check=True)
    return sorted(glob.glob(os.path.join(out_dir, "page-*.png")))


def split_pdf_v2(pdf: str, out_dir: str, dpi: int) -> list:
    import fitz

    os.makedirs(out_dir, exist_ok=True)
    pages = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf) as doc:
        for index, page in enumerate(doc, start=1):
            out_path = os.path.join(out_dir, f"page-{index:02d}.png")
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pixmap.save(out_path)
            pages.append(out_path)

    return pages


def make_client():
    """Opus client, wired to EOB v9 auth (KV -> Foundry), else env credentials."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        if d and d not in sys.path:
            sys.path.insert(0, d)
    try:
        from auth import get_anthropic_client, get_kv_client
        return get_anthropic_client(get_kv_client())  # EOB KV -> Foundry
    except Exception as e:
        print(f"[warn] EOB auth.py path unavailable ({e}); using env credentials")

    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    base = os.environ.get("ANTHROPIC_BASE_URL")
    if not key:
        raise SystemExit(
            "\nNo credentials resolved. Do ONE of:\n"
            "  1) Run from your EOB_v9 folder (so auth.py's KV->Foundry works) "
            "after `az login`.\n"
            "  2) Set ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL).\n")
    if base:
        return anthropic.AnthropicFoundry(api_key=key, base_url=base)
    return anthropic.Anthropic(api_key=key)


def process_pdf(pdf_path: str, client, registry, *, pages_dir: str = "pages",
                want=None, auto_build: bool = True, on_page=None):
    """Split -> per-page extract -> differentiate. Returns (results, metrics).

    on_page, if given, is called after each page result is finalized as
        on_page(page_number: int, page_image_path: str, result: dict)
    which is how api.py uploads the page to blob and persists it to Postgres
    per page. registry may be mutated in place when a new template is built.
    Render DPI is fixed (RENDER_DPI).
    """
    import numpy as np
    pages = split_pdf_v2(pdf_path, pages_dir, RENDER_DPI)

    # Build the ink-isolation template once (median of aligned pages). The
    # cleaned per-page ink image is sent to the extractor as mark evidence so
    # stray printed-ink bleed can't be misread as a selection.
    template = aligned = None
    try:
        if len(pages) >= 4:
            from mark_detect import build_template
            template, aligned = build_template(pages)
    except Exception as e:
        print(f"[warn] ink-isolation unavailable ({e}); single-image extraction")

    results, seen_identity = [], {}
    for i, path in enumerate(pages, start=1):
        if want and i not in want:
            continue

        seen_labels, seen_codes = identify_page(path, client)
        cat_path, catalog, sc = pick_catalog(seen_labels, seen_codes, registry)
        tstate = "known" if sc >= STRONG else "miss"

        if tstate == "miss" and not seen_labels and not seen_codes and registry:
            cat_path, catalog = registry[0]
            tstate = "known"

        if tstate == "miss":
            if not auto_build:
                results.append({"page": i, "flags": ["template_miss_skipped"],
                                "template_match": {"state": "miss", "score": round(sc, 2)}})
                if on_page:
                    on_page(i, path, results[-1])
                continue
            key = "|".join(sorted(_norm(s) for s in seen_labels)) or "unknown"
            tid = "autobuilt_" + hashlib.md5(key.encode()).hexdigest()[:8]
            cat_path = f"catalog_{tid}.json"
            if not os.path.exists(cat_path):
                build_catalog(path, client, page=1, out=cat_path, template_id=tid)
            catalog = json.load(open(cat_path))
            registry.append((cat_path, catalog))
            tstate = "autobuilt"

        if aligned is not None:
            g = aligned[i - 1]
            raw_b64 = array_b64(g.astype("uint8"))
            d = np.clip(template - g, 0, 255)
            d[d < 35] = 0
            ink = (255 - d).astype("uint8")
            r = extract_page(raw_b64, catalog, client, marks_b64=array_b64(ink))
        else:
            r = extract_page(load_page_b64(path), catalog, client)

        r["template_match"] = {"state": tstate, "score": round(sc, 2),
                               "catalog": os.path.basename(cat_path),
                               "template_id": catalog.get("template_id")}

        h = r.get("header", {})
        ident = (h.get("name", "").strip().lower(), h.get("dob", "").strip())
        if not any(ident):
            r.setdefault("flags", []).append("blank_header")
        elif ident in seen_identity:
            r.setdefault("flags", []).append(f"duplicate_of_page_{seen_identity[ident]}")
        else:
            seen_identity[ident] = i

        r["page"] = i
        results.append(r)
        if on_page:
            on_page(i, path, r)
        print(f"page {i:>2}: {h.get('name','?'):<28} [{tstate}] "
              f"proc={len(r.get('circled_procedures',[]))} "
              f"dx={len(r.get('circled_diagnoses',[]))} "
              f"notes={len(r.get('notes',[]))} flags={r.get('flags',[])}")

    metrics = build_metrics(pdf_path, results)
    return results, metrics


def build_metrics(pdf_path: str, results: list) -> dict:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    metrics = {
        "timestamp": ts, "source_pdf": os.path.basename(pdf_path),
        "pages_processed": len(results),
        "pages_flagged": sum(1 for r in results if r.get("flags")),
        "pages_autobuilt": sum(1 for r in results
                               if r.get("template_match", {}).get("state") == "autobuilt"),
        "total_procedures": sum(len(r.get("circled_procedures", [])) for r in results),
        "total_diagnoses": sum(len(r.get("circled_diagnoses", [])) for r in results),
        "total_notes": sum(len(r.get("notes", [])) for r in results),
        "flag_counts": {},
    }
    for r in results:
        for f in r.get("flags", []):
            metrics["flag_counts"][f] = metrics["flag_counts"].get(f, 0) + 1
    return metrics


def default_output_path(pdf_path: str) -> str:
    folder = os.path.dirname(pdf_path) or "."
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    stamp = datetime.datetime.now().strftime("%I-%M%p").lstrip("0").lower()
    return os.path.join(folder, f"results-{stem}-{stamp}.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--pages", default="", help="1-based comma list, blank=all")
    ap.add_argument("--out", default=None,
                    help="output JSON path; defaults to results-<input-stem>.json next to the input PDF")
    ap.add_argument("--no-auto-build", action="store_true",
                    help="don't build a catalog for an unknown form; skip instead")
    args = ap.parse_args()
    out_path = args.out or default_output_path(args.pdf)

    want = {int(x) for x in args.pages.split(",") if x.strip()} if args.pages else None
    client = make_client()
    registry = load_registry(args.catalog)

    results, metrics = process_pdf(
        args.pdf, client, registry, want=want,
        auto_build=not args.no_auto_build)

    json.dump({"source_pdf": os.path.basename(args.pdf),
               "page_count": len(results), "pages": results},
              open(out_path, "w"), indent=2)
    json.dump(metrics, open(f"run_metrics_{metrics['timestamp']}.json", "w"), indent=2)
    print(f"\nwrote {out_path}  ({len(results)} pages)  + run_metrics_{metrics['timestamp']}.json")


if __name__ == "__main__":
    main()
