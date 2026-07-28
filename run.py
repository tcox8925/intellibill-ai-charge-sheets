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

Needs: pip install anthropic pymupdf pillow
Set ANTHROPIC_API_KEY (prototype only; in-tenant use your KV-backed client).
"""

import argparse, json, os, re, sys, glob, hashlib, datetime
from catalog_paths import catalog_glob_pattern, catalog_input_path, catalog_output_path
from extract import (extract_page, identify_page, load_page_b64, array_b64,
                     detect_orientation)
from fingerprint import STRONG, score, _norm
from build_catalog import build_catalog
from pdf_raster import render_pdf_pages

# Raster resolution is FIXED, not configurable: 200 DPI for page extraction
# (a letter page lands ~2200px, matching extract.py's max_px cap) and 300 DPI
# for the one-time catalog build (build_catalog's own default). Raising these
# without also raising extract.max_px is wasted, so they stay baked in.
RENDER_DPI = 200

# --- charge-sheet recognition (is this page even a charge sheet?) -----------
# The fingerprint decides WHICH template; this decides IF the page is a charge
# sheet at all, so non-charge-sheets (cover pages, faxes, EOBs, blanks) are
# rejected instead of force-fit onto a catalog. Trust the model's judgment when
# it's confident; otherwise fall back to billing-code density.
CS_CONF_MIN = 0.55     # model confidence needed to accept/reject on its say-so
MIN_CODES   = 4        # billing-code-shaped tokens that, with a header, imply a charge sheet


def _codeish(tok: str) -> bool:
    t = (tok or "").strip().upper()
    return bool(re.match(r"^\d{5}$", t)            # CPT
                or re.match(r"^[A-Z]\d{4}$", t)    # HCPCS
                or re.match(r"^[A-TV-Z]\d", t))    # ICD-10


def looks_like_chargesheet(is_cs, conf, labels, codes) -> bool:
    """Recognition gate. is_cs/conf come from the Haiku identify pass; labels/
    codes are what it read off the printed form."""
    if is_cs is True and conf >= CS_CONF_MIN:
        return True
    if is_cs is False and conf >= CS_CONF_MIN:
        return False
    # model unsure (or failed) -> density backstop: a grid of billing codes
    code_like = sum(1 for c in (codes or []) if _codeish(c))
    return code_like >= MIN_CODES and len(labels or []) >= 1


def load_registry(explicit: str = None) -> list:
    """Load every known catalog from `catalogues/` into a registry. A rescan of
    a known form matches on content anchors and reuses it — no rebuild."""
    reg = {}
    explicit_path = None
    if explicit:
        resolved = catalog_input_path(explicit)
        if os.path.exists(resolved):
            explicit_path = resolved
    paths = ([explicit_path] if explicit_path else []) + \
            sorted(glob.glob(catalog_glob_pattern()))
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
    return render_pdf_pages(pdf, out_dir, dpi)


def normalize_orientation(pages: list, client) -> dict:
    """Detect each page's rotation and rewrite it upright, in place. Returns
    {page_number: {...}}. Detection is a cheap per-page Haiku call
    (extract.detect_orientation), fallback 90 if it can't reach the model.

    BATCH CONSENSUS: a practice's scanned superbills all share one orientation.
    If a strong majority agree, that mode is treated as ground truth and lone
    outliers are overridden to it. Overrides are flagged."""
    from collections import Counter
    from PIL import Image

    raw = {
        i: detect_orientation(path, client)
        for i, path in enumerate(pages, start=1)
    }
    counts = Counter(raw.values())
    mode, mode_n = counts.most_common(1)[0]
    dominant = mode_n >= max(3, int(0.6 * len(pages)))

    info = {}
    for i, path in enumerate(pages, start=1):
        deg, overridden = raw[i], False
        if dominant and deg != mode:
            deg, overridden = mode, True
        if deg:
            Image.open(path).rotate(deg, expand=True).save(path)
        info[i] = {"applied_rotation_deg": deg, "detected": True, "method": "haiku",
                   "raw_detected_deg": raw[i]}
        if overridden:
            info[i]["overridden_to_batch_mode"] = True
    return info


def dominant_catalog(template_counts: dict):
    """(cat_path, catalog) of the template most pages in this run matched, or
    None. Used to fail a recognition-failed page OPEN onto the batch's real
    template instead of dropping it or auto-building junk."""
    if not template_counts:
        return None
    tid = max(template_counts, key=lambda key: template_counts[key][0])
    return template_counts[tid][1]


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

    pages = split_pdf(pdf_path, pages_dir, RENDER_DPI)
    page_orient = normalize_orientation(pages, client)

    template = aligned = None
    try:
        if len(pages) >= 4:
            from mark_detect import build_template
            template, aligned = build_template(pages)
    except Exception as e:
        print(f"[warn] ink-isolation unavailable ({e}); single-image extraction. "
              "For mark-precision, install: pip install opencv-python-headless scipy")

    results, seen_identity, template_counts = [], {}, {}
    for i, path in enumerate(pages, start=1):
        if want and i not in want:
            continue

        orient = page_orient.get(i, {"applied_rotation_deg": 0, "detected": False})
        seen_labels, seen_codes, is_cs, cs_conf = identify_page(path, client)
        cat_path, catalog, sc = pick_catalog(seen_labels, seen_codes, registry)

        recognition_failed = (is_cs is None and not seen_labels and not seen_codes)
        recognized = looks_like_chargesheet(is_cs, cs_conf, seen_labels, seen_codes) \
                     or sc >= STRONG

        failopen = False
        if not recognized and recognition_failed:
            dom = dominant_catalog(template_counts)
            if dom is not None:
                cat_path, catalog = dom
                sc, failopen = STRONG, True

        if not recognized and not failopen:
            rej = {
                "page": i,
                "template_ok": False,
                "orientation": orient,
                "header": {},
                "circled_procedures": [],
                "circled_diagnoses": [],
                "possible_marks": [],
                "notes": [],
                "recognition": {
                    "is_chargesheet": False,
                    "confidence": cs_conf,
                    "seen_sections": seen_labels,
                    "seen_codes_sample": (seen_codes or [])[:8],
                },
                "flags": ["not_chargesheet", "skipped_no_extraction"],
                "template_match": {"state": "not_chargesheet", "score": round(sc, 2)},
            }
            if recognition_failed:
                rej["flags"].append("recognition_failed")
            results.append(rej)
            if on_page:
                on_page(i, path, rej)
            tag = "recognition failed, no template" if recognition_failed else \
                  f"NOT a charge sheet (conf={cs_conf:.2f})"
            print(f"page {i:>2}: {tag} — skipped")
            continue

        tstate = "known" if sc >= STRONG else "miss"

        if tstate == "miss":
            if not auto_build:
                results.append({"page": i, "flags": ["unknown_template_skipped"],
                                "template_match": {"state": "miss", "score": round(sc, 2)}})
                if on_page:
                    on_page(i, path, results[-1])
                continue
            key = "|".join(sorted(_norm(s) for s in seen_labels)) or "unknown"
            tid = "autobuilt_" + hashlib.md5(key.encode()).hexdigest()[:8]
            cat_path = catalog_output_path(f"catalog_{tid}.json")
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
        r["recognition"] = {"is_chargesheet": True, "confidence": cs_conf}
        r["orientation"] = orient

        if tstate == "known":
            tid = catalog.get("template_id")
            slot = template_counts.setdefault(tid, [0, (cat_path, catalog)])
            slot[0] += 1

        if orient.get("overridden_to_batch_mode"):
            r.setdefault("flags", []).append("orientation_overridden")
        if failopen:
            r.setdefault("flags", []).append("recognition_failed_failopen")
        if r.get("template_ok") is False:
            r.setdefault("flags", []).append("template_mismatch")

        h = r.get("header", {})
        for flag in check_dates(h):
            r.setdefault("flags", []).append(flag)
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
              f"proc={len(r.get('circled_procedures', []))} "
              f"dx={len(r.get('circled_diagnoses', []))} "
              f"notes={len(r.get('notes', []))} flags={r.get('flags', [])}")

    metrics = build_metrics(pdf_path, results)
    return results, metrics


def _parse_date(s):
    """Parse an M/D/Y date in mixed separators. Returns
    (date | None, normalized 'MM-DD-YYYY', year_was_2digit, status) where status
    is 'ok' | 'unparseable' | 'impossible'. 2-digit years are pivoted on the
    current year (<= this year -> 20xx, else 19xx)."""
    parts = [p for p in re.split(r"[^0-9]+", (s or "").strip()) if p]
    if len(parts) != 3:
        return None, s, False, "unparseable"
    try:
        m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None, s, False, "unparseable"
    two_digit = len(parts[2]) <= 2
    if two_digit:
        cur2 = datetime.date.today().year % 100
        y = 2000 + y if y <= cur2 else 1900 + y
    try:
        dt = datetime.date(y, m, d)
    except ValueError:
        return None, s, two_digit, "impossible"
    return dt, f"{m:02d}-{d:02d}-{y:04d}", two_digit, "ok"


def check_dates(header: dict) -> list:
    """Deterministic sanity checks on the header date + DOB."""
    flags, today = [], datetime.date.today()

    svc, svc_norm, svc_2d, svc_status = _parse_date(header.get("date"))
    if header.get("date"):
        if svc_status == "unparseable":
            flags.append("service_date_unparseable")
        elif svc_status == "impossible":
            flags.append("service_date_impossible")
        else:
            if svc_2d:
                header["date"] = svc_norm
                flags.append("service_date_year_normalized")
            if svc > today:
                flags.append("service_date_future")

    dob, dob_norm, dob_2d, dob_status = _parse_date(header.get("dob"))
    if header.get("dob"):
        if dob_status == "unparseable":
            flags.append("dob_unparseable")
        elif dob_status == "impossible":
            flags.append("dob_impossible")
        else:
            if dob_2d:
                header["dob"] = dob_norm
                flags.append("dob_year_normalized")
            if dob > today:
                flags.append("dob_future")
            elif dob.year < 1900 or (today - dob).days / 365.25 > 120:
                flags.append("dob_implausible")
            if svc_status == "ok" and dob > svc:
                flags.append("dob_after_service_date")
    return flags


def orientation_info(results: list) -> dict:
    """Document-level orientation summary. Orientation is now DETECTED per page
    (extract.detect_orientation) and each page normalized to upright before
    extraction; the per-page value lives in pages[].orientation. This summary
    reports the method and the distribution of applied rotations."""
    dist = {}
    for r in results:
        deg = (r.get("orientation") or {}).get("applied_rotation_deg")
        if deg is not None:
            dist[str(deg)] = dist.get(str(deg), 0) + 1
    return {
        "detected": True,
        "method": "per_page_haiku",
        "result": "upright",
        "applied_rotation_deg_distribution": dist,
    }


def build_metrics(pdf_path: str, results: list) -> dict:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    def _state(result):
        return result.get("template_match", {}).get("state")

    not_cs = sum(1 for r in results if _state(r) == "not_chargesheet")
    extracted = sum(1 for r in results
                    if _state(r) in ("known", "autobuilt") and "_error" not in r)
    metrics = {
        "timestamp": ts,
        "source_pdf": os.path.basename(pdf_path),
        "orientation": orientation_info(results),
        "pages_processed": len(results),
        "pages_extracted": extracted,
        "pages_not_chargesheet": not_cs,
        "pages_flagged": sum(1 for r in results if r.get("flags")),
        "pages_autobuilt": sum(1 for r in results if _state(r) == "autobuilt"),
        "total_procedures": sum(len(r.get("circled_procedures", [])) for r in results),
        "total_diagnoses": sum(len(r.get("circled_diagnoses", [])) for r in results),
        "total_notes": sum(len(r.get("notes", [])) for r in results),
        "flag_counts": {},
    }
    if extracted == 0:
        metrics["document_warnings"] = ["no_chargesheet_pages"]
    for r in results:
        for f in r.get("flags", []):
            metrics["flag_counts"][f] = metrics["flag_counts"].get(f, 0) + 1
    return metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--pages", default="", help="1-based comma list, blank=all")
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--no-auto-build", action="store_true",
                    help="don't build a catalog for an unknown form; skip instead")
    args = ap.parse_args()

    want = {int(x) for x in args.pages.split(",") if x.strip()} if args.pages else None
    client = make_client()
    registry = load_registry(args.catalog)

    results, metrics = process_pdf(
        args.pdf, client, registry, want=want,
        auto_build=not args.no_auto_build)

    json.dump({"source_pdf": os.path.basename(args.pdf),
               "page_count": len(results),
               "orientation": orientation_info(results),
               "pages": results},
              open(args.out, "w"), indent=2)
    json.dump(metrics, open(f"run_metrics_{metrics['timestamp']}.json", "w"), indent=2)
    print(f"\nwrote {args.out}  ({len(results)} pages)  + run_metrics_{metrics['timestamp']}.json")


if __name__ == "__main__":
    main()
