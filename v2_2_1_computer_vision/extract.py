"""AI extraction for variable header fields and handwritten clinical notes only.

Selected billing codes are intentionally absent from the model schema. Runtime
code selection is performed only by locked_template.py.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
from typing import Any

from PIL import Image

from settings import get_settings

HEADER_NOTES_SYSTEM = (
    "You transcribe variable handwritten demographic/payment fields and handwritten "
    "clinical notes from a scanned medical charge sheet. Return strict JSON only. "
    "Never identify, infer, report, or discuss selected CPT, HCPCS, or ICD codes. "
    "Ignore circles, checks, underlines, and marks around billing codes; billing-code "
    "selection is handled by deterministic software outside this model."
)

HEADER_NOTES_PROMPT = """Return JSON only with exactly this shape:
{"header":{"date":"","name":"","dob":"","prn":"","insurance":"","copay":"","amount_paid":"","payment_type":""},"notes":[{"text":"","near":"","confidence":0.0}],"flags":[]}
Rules:
- Transcribe the header/payment values visible on the page. Do not invent missing text.
- notes are handwritten CLINICAL notes only.
- Do not repeat the patient name, date, DOB, PRN, insurance, payment text, signatures,
  staff names, page numbers, timestamps, or selected billing codes as notes.
- If uncertain, leave a field blank or omit a note and add "illegible_handwriting".
- Never return circled_procedures, circled_diagnoses, possible_marks, selected_codes,
  CPT, HCPCS, or ICD arrays.
"""

_TRANSIENT = (
    "429", "rate limit", "rate_limit", "peer closed", "connection reset",
    "remoteprotocolerror", "timed out", "timeout", "overloaded", "server disconnected",
)


def image_b64(rgb_array) -> str:
    buf = io.BytesIO()
    Image.fromarray(rgb_array).convert("RGB").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def _clean(result: dict[str, Any]) -> dict:
    header = result.get("header") if isinstance(result.get("header"), dict) else {}
    keys = ("date", "name", "dob", "prn", "insurance", "copay", "amount_paid", "payment_type")
    clean_header = {k: str(header.get(k) or "").strip() for k in keys}

    notes, seen = [], set()
    for n in result.get("notes") or []:
        if not isinstance(n, dict):
            continue
        text = re.sub(r"\s+", " ", str(n.get("text") or "")).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            confidence = max(0.0, min(1.0, float(n.get("confidence", 0.0))))
        except Exception:
            confidence = 0.0
        notes.append({
            "text": text,
            "near": re.sub(r"\s+", " ", str(n.get("near") or "")).strip(),
            "confidence": round(confidence, 3),
        })

    flags = []
    for f in result.get("flags") or []:
        f = str(f).strip()
        if f and f not in flags:
            flags.append(f)
    return {"header": clean_header, "notes": notes, "flags": flags}


def extract_header_notes(rgb_array, client, retries: int = 3) -> dict:
    s = get_settings()
    if not s.enable_header_notes:
        return {"header": {}, "notes": [], "flags": ["header_notes_disabled"]}

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64(rgb_array)}},
        {"type": "text", "text": HEADER_NOTES_PROMPT},
    ]
    last_error = None
    for attempt in range(retries + 1):
        try:
            msg = client.messages.create(
                model=s.header_model,
                max_tokens=1200,
                temperature=0,
                system=HEADER_NOTES_SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            text = "".join(getattr(block, "text", "") for block in msg.content if getattr(block, "type", "") == "text")
            result = _clean(_parse_json(text))
            for forbidden in ("circled_procedures", "circled_diagnoses", "possible_marks", "selected_codes"):
                result.pop(forbidden, None)
            return result
        except Exception as exc:
            last_error = exc
            transient = any(p in str(exc).lower() for p in _TRANSIENT)
            if transient and attempt < retries:
                time.sleep(min(8, 2 ** attempt))
                continue
            if attempt < 1:
                continue
            break
    return {
        "header": {},
        "notes": [],
        "flags": ["header_notes_extraction_failed"],
        "_header_error": str(last_error),
    }
