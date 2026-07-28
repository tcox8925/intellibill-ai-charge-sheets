"""
extract.py — per-page extraction for the NWA Internal Medicine superbill.

Design (mirrors the EOB v9 pattern: single vision call per page, catalog as
context, validate output; retry on parse failure; local JSON output for now):

  1. rotate page upright (the scans render 90° rotated)
  2. build a prompt that INJECTS the template catalog, so the model PICKS from
     known codes rather than transcribing a grid  -> kills a class of halluc.
  3. one Opus vision call -> strict JSON
  4. validate every returned code exists in the catalog (hard guard)

Everything is local. No DB writes. PHI stays on disk in this prototype.
In-tenant you would swap the client for your Azure OpenAI/Anthropic deployment
and keep KV/VNet auth exactly like pch-eob-pipeline.
"""

import base64, io, json, logging, os, re, time
from PIL import Image

logger = logging.getLogger("chargesheet.extract")

# Mirror EOB v9 auth.py: Opus via AnthropicFoundry, model string from auth.
# Falls back to a plain string if auth.py isn't importable (e.g. this sandbox).
try:
    from auth import OPUS_MODEL as MODEL
except Exception:
    MODEL = os.environ.get("CHARGE_MODEL", "claude-opus-4-6")

try:
    from auth import HAIKU_MODEL
except Exception:
    HAIKU_MODEL = os.environ.get("CHARGE_HAIKU", "claude-haiku-4-5")

ROTATE_DEG = int(os.environ.get("CHARGE_ROTATE", "0"))  # manual override only;
# run.py DETECTS orientation per page and normalizes each to upright before
# extraction, so the default is 0. Set CHARGE_ROTATE to force a fixed rotation
# (extract, mark_detect and build_catalog all honor the same env var).

# EOB v9 transient-error patterns — retry these, fail fast on everything else.
TRANSIENT_PATTERNS = [
    "429", "rate_limit", "rate limit", "peer closed connection",
    "connection reset", "RemoteProtocolError", "complete message body",
    "server disconnected", "read timeout", "timed out", "overloaded",
]


def _is_transient(err: Exception) -> bool:
    s = str(err).lower()
    return any(p.lower() in s for p in TRANSIENT_PATTERNS)


# ---------- prompt ----------------------------------------------------------

SYSTEM = (
    "You read a scanned handwritten medical superbill (charge sheet) and return "
    "STRICT JSON only. No prose, no markdown, no code fences. "
    "You are given the printed template CATALOG of every code on the form. "
    "You must ONLY report codes that appear in that catalog — never invent a code. "
    "Your job: (a) read the handwritten header fields, (b) decide which catalog "
    "codes are DELIBERATELY marked (a hand-drawn circle, check, tick, or "
    "underline clearly on or around the code), and (c) transcribe handwritten "
    "clinical notes. "
    "CRITICAL — do NOT over-report. Ink bleed or descenders from handwriting "
    "(e.g. the patient's name written across the sheet), stray pen strokes, "
    "smudges, doodles, and the pre-printed grid lines are NOT selections. "
    "The patient's own name is NOT a clinical note. When unsure whether a mark "
    "is deliberate, it is NOT a selection."
)


def build_user_prompt(catalog: dict) -> str:
    # Compact the catalog so the model sees code -> description per section.
    lines = []
    for sec in catalog["sections"]:
        lines.append(f"## {sec['section']} ({sec['code_type']})")
        for c in sec["cells"]:
            lines.append(f"{c['code']}\t{c['description']}")
    catalog_block = "\n".join(lines)

    schema = {
        "template_ok": "true|false — do the printed section headers/codes match this form?",
        "header": {
            "date": "", "name": "", "dob": "", "prn": "",
            "insurance": "", "copay": "", "amount_paid": "", "payment_type": ""
        },
        "circled_procedures": [
            {"code": "", "description": "", "section": "",
             "mark": "circle|check|underline", "confidence": 0.0}
        ],
        "circled_diagnoses": [
            {"code": "", "description": "", "section": "",
             "mark": "circle|check|underline", "confidence": 0.0}
        ],
        "possible_marks": [
            {"code": "", "description": "", "section": "",
             "reason": "why it's uncertain (faint / near handwriting / stray)",
             "confidence": 0.0}
        ],
        "notes": [
            {"text": "", "near": "code or region it sits by, or 'margin'",
             "confidence": 0.0}
        ],
        "flags": ["ambiguous_mark|illegible_handwriting|blank_header|other"]
    }

    return (
        "TEMPLATE CATALOG (code<TAB>description, grouped by section):\n"
        f"{catalog_block}\n\n"
        "Return JSON with EXACTLY this shape (fill values; omit empty arrays as []):\n"
        f"{json.dumps(schema, indent=2)}\n\n"
        "Rules:\n"
        "- Only codes present in the catalog above may appear in the output.\n"
        "- circled_procedures / circled_diagnoses are for CONFIRMED deliberate "
        "marks only (a clear circle/check/tick/underline), confidence >= 0.75. "
        "CPT/HCPCS -> circled_procedures, ICD-10 -> circled_diagnoses.\n"
        "- If you are NOT sure a mark is deliberate (faint, near handwriting, a "
        "possible stray stroke), put it in possible_marks — NOT in the circled_ "
        "lists — and add 'ambiguous_mark' to flags. When in doubt, possible_marks.\n"
        "- Do NOT report ink bleed, name descenders, smudges, or grid lines as marks.\n"
        "- notes = handwritten CLINICAL text only (e.g. 'R knee pain', 'both legs "
        "x3'). Do NOT put the patient's name, the date, or page numbers in notes.\n"
        "- Header names/DOB/PRN are handwritten — transcribe literally, do not correct.\n"
    )


# ---------- image prep ------------------------------------------------------

def load_page_b64(path: str, rotate: int = ROTATE_DEG, max_px: int = 2200) -> str:
    im = Image.open(path)
    if rotate:
        im = im.rotate(rotate, expand=True)
    if max(im.size) > max_px:
        s = max_px / max(im.size)
        im = im.resize((int(im.width * s), int(im.height * s)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def array_b64(arr) -> str:
    """uint8 HxW (grayscale) or HxWx3 array -> base64 PNG."""
    buf = io.BytesIO()
    Image.fromarray(arr).convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------- validation (hallucination guard) --------------------------------

def catalog_code_set(catalog: dict) -> set:
    return {c["code"] for sec in catalog["sections"] for c in sec["cells"]}


def validate(result: dict, catalog: dict) -> dict:
    valid = catalog_code_set(catalog)
    dropped = []
    for key in ("circled_procedures", "circled_diagnoses"):
        kept = []
        for item in result.get(key, []) or []:
            if item.get("code") in valid:
                kept.append(item)
            else:
                dropped.append(item.get("code"))
        result[key] = kept
    if dropped:
        result.setdefault("flags", []).append("dropped_noncatalog_codes")
        result["_dropped_codes"] = dropped
    return result


CONFIRM_MIN = 0.75   # below this a mark isn't a confirmed selection


def refine(result: dict) -> dict:
    """Precision net: keep circled_* to CONFIRMED deliberate marks only; push
    'other'/low-confidence marks to possible_marks. Drop notes that just echo
    the patient's name/date (not clinical text)."""
    possible = result.get("possible_marks", []) or []
    for key in ("circled_procedures", "circled_diagnoses"):
        kept = []
        for m in result.get(key, []) or []:
            if m.get("mark") == "other" or float(m.get("confidence", 1)) < CONFIRM_MIN:
                m["reason"] = m.get("mark") or "low_confidence"
                possible.append(m)
            else:
                kept.append(m)
        result[key] = kept
    if possible:
        result["possible_marks"] = possible
        result.setdefault("flags", []).append("has_possible_marks")

    # strip notes that merely repeat the header name / date / page number
    h = result.get("header", {})
    name_toks = {t for t in re.split(r"\W+", (h.get("name") or "").lower()) if len(t) > 2}
    clean = []
    for n in result.get("notes", []) or []:
        txt = (n.get("text") or "").lower()
        toks = {t for t in re.split(r"\W+", txt) if len(t) > 2}
        overlap = (len(toks & name_toks) / len(toks)) if toks else 0.0
        is_name_echo = overlap >= 0.5          # mostly the patient's name
        is_meta = bool(re.fullmatch(r"[\d\s/\.:-]+", txt.strip())) or not txt.strip()
        if not (is_name_echo or is_meta):
            clean.append(n)
    result["notes"] = clean
    return result


# ---------- the call --------------------------------------------------------

def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


IDENTIFY_SYSTEM = (
    "You look at a scanned medical form and list only the PRINTED section "
    "headers and a few printed billing codes you can see. Strict JSON, no prose."
)

ORIENT_SYSTEM = (
    "You determine the rotation of a scanned page. Strict JSON, no prose."
)


def clockwise_restoration_rotation(raw_clockwise_deg: int) -> int:
    """Clockwise rotation needed to restore an image to upright."""
    return (-int(raw_clockwise_deg)) % 360


def detect_orientation(path: str, client) -> int:
    """Cheap Haiku pass on the raw page. Returns `rotate_ccw` as 0/90/180/270,
    defaulting to 0 on failure or invalid output."""
    img = load_page_b64(path, rotate=0)   # RAW — do not pre-rotate
    prompt = (
        'Return JSON only: {"rotate_ccw": 0|90|180|270}.\n'
        "rotate_ccw = degrees to rotate the image COUNTER-CLOCKWISE so the "
        "printed text becomes upright and reads left-to-right (0 if already upright)."
    )
    try:
        msg = client.messages.create(
            model=HAIKU_MODEL, max_tokens=50, system=ORIENT_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/png", "data": img}},
                {"type": "text", "text": prompt}]}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        payload = _parse_json(text)
        # logger.info("Orientation payload: %s", payload)
        deg = int(payload.get("rotate_ccw", 0)) % 360
        return deg if deg in (0, 90, 180, 270) else 0
    except Exception:
        # logger.exception("Orientation detection failed for %s", path)
        return 0


def identify_page(path: str, client, retries: int = 2) -> tuple:
    """Cheap Haiku pass: which printed section headers + codes are on this page,
    AND is this even a charge sheet? Returns
        (seen_sections, seen_codes, is_chargesheet, confidence)
    Feeds the fingerprint so run.py can pick a known catalog or build a new one,
    and the recognition gate so non-charge-sheets are rejected, not force-fit.
    Retries transient errors AND empty parses — a blank response here otherwise
    looks like 'not a charge sheet' and a real sheet gets dropped.
    Mirrors EOB Stage-1 page classification (Haiku, not Opus)."""
    img = load_page_b64(path)
    prompt = (
        'Return JSON only: {"is_chargesheet": true|false, "confidence": 0.0, '
        '"seen_sections": ["..."], "seen_codes": ["..."]}.\n'
        "is_chargesheet: true ONLY if this page is a medical superbill / charge "
        "sheet — a printed grid of billing codes (CPT/HCPCS/ICD-10) under section "
        "headers, meant for marking services rendered. A cover page, fax banner, "
        "insurance EOB/remittance, letter, or blank page is NOT a charge sheet.\n"
        "confidence: 0.0-1.0 for the is_chargesheet judgment.\n"
        "seen_sections: the printed section-header titles on the form.\n"
        "seen_codes: up to 15 printed billing codes you can read "
        "(e.g. 99214, J1885, M54.5). Ignore all handwriting, circles, notes."
    )
    for attempt in range(retries + 1):
        try:
            msg = client.messages.create(
                model=HAIKU_MODEL, max_tokens=800, system=IDENTIFY_SYSTEM,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                     "media_type": "image/png", "data": img}},
                    {"type": "text", "text": prompt}]}],
            )
            text = "".join(b.text for b in msg.content if b.type == "text")
            d = _parse_json(text)
            secs = d.get("seen_sections") or []
            codes = d.get("seen_codes") or []
            is_cs = d.get("is_chargesheet")
            conf = float(d.get("confidence") or 0.0)
            # a wholly empty read is almost always a transient/parse blip — retry
            if is_cs is None and not secs and not codes and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return secs, codes, is_cs, conf
        except Exception as e:
            if attempt < retries:
                if _is_transient(e):
                    time.sleep(2 ** attempt)
                continue
    return [], [], None, 0.0


DUAL_IMAGE_NOTE = (
    "\nYou are given TWO images of the SAME sheet in the SAME frame:\n"
    "  IMAGE 1 = the original page — use it to read printed codes, the section "
    "layout, header fields, and handwriting.\n"
    "  IMAGE 2 = ink-isolated — the printed form has been removed, leaving ONLY "
    "hand-written ink (circles, checks, squiggles, notes) as dark strokes on "
    "white. Use IMAGE 2 as the EVIDENCE for what is marked: a code counts as "
    "selected ONLY if a deliberate hand mark sits over its position in IMAGE 2. "
    "If a spot is blank/faint in IMAGE 2, it is NOT selected (this removes stray "
    "printed-ink bleed). Map the mark's position back to the code in IMAGE 1.\n"
)


def extract_page(raw_b64, catalog: dict, client, marks_b64=None,
                 retries: int = 3) -> dict:
    """One Opus vision call per page. If marks_b64 (ink-isolated image) is
    provided, both images are sent and the model uses the ink image as the
    evidence for which codes are marked. Retries transient errors."""
    user = build_user_prompt(catalog)
    content = [{"type": "image", "source": {"type": "base64",
                "media_type": "image/png", "data": raw_b64}}]
    if marks_b64:
        content.append({"type": "image", "source": {"type": "base64",
                        "media_type": "image/png", "data": marks_b64}})
        user = user + DUAL_IMAGE_NOTE
    content.append({"type": "text", "text": user})

    last_err = None
    for attempt in range(retries + 1):
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=2000, system=SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            text = "".join(b.text for b in msg.content if b.type == "text")
            result = _parse_json(text)
            return refine(validate(result, catalog))
        except Exception as e:
            last_err = e
            if _is_transient(e) and attempt < retries:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s...
                continue
            if attempt < 1:      # non-transient (e.g. parse): one clean re-ask
                continue
            break
    return {"template_ok": None, "header": {}, "circled_procedures": [],
            "circled_diagnoses": [], "notes": [],
            "flags": ["extraction_failed"], "_error": str(last_err)}
