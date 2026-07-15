"""
fingerprint.py — template identity by CONTENT, not pixel position.

Identity is anchored on the PRINTED SECTION HEADERS, which are byte-identical on
every page of a given form and never contain handwriting/names. Printed codes
are a weak secondary signal (they help tell apart two forms with similar section
names). We deliberately do NOT key on the per-page code readout, because the
identify pass reports a different code subset each page — keying on it made every
page look like a new template.

    known   -> score >= STRONG          : reuse this catalog (incl. rescans)
    unknown -> score <  WEAK            : new form -> build a catalog
    fuzzy   -> WEAK <= score < STRONG   : ambiguous

Header-dominant weighting: a same-form page matches even if the identify pass
missed a few headers and reported zero overlapping codes.
"""

STRONG = 0.55   # 0.8 * ~0.69 header overlap clears this
WEAK   = 0.35


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def score(seen_labels, seen_codes, catalog: dict) -> float:
    fp = catalog["fingerprint"]
    want_labels = {_norm(x) for x in fp["header_labels"]}
    want_codes  = {_norm(x) for x in fp["anchor_codes"]}
    got_labels  = {_norm(x) for x in (seen_labels or [])}
    got_codes   = {_norm(x) for x in (seen_codes or [])}
    if not want_labels:
        return 0.0
    lab_hit = len(want_labels & got_labels) / len(want_labels)
    cod_hit = (len(want_codes & got_codes) / len(want_codes)) if want_codes else 0.0
    # section headers dominate identity; codes are a light tie-breaker
    return 0.8 * lab_hit + 0.2 * cod_hit


def classify(seen_labels, seen_codes, catalog: dict) -> tuple:
    s = score(seen_labels, seen_codes, catalog)
    if s >= STRONG:
        return "known", s
    if s < WEAK:
        return "unknown", s
    return "fuzzy", s
