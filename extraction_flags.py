"""Shared extraction flag/template-state string constants.

These are produced by run.py and image_ocr.py (the two per-page extraction
paths) and consumed by db.py (to decide a child attachment's status). Kept
in one place so the two producers and the one consumer can't drift apart.
"""

FLAG_NOT_CHARGESHEET = "not_chargesheet"
FLAG_SKIPPED_NO_EXTRACTION = "skipped_no_extraction"
FLAG_BLANK_HEADER = "blank_header"
FLAG_RECOGNITION_FAILED = "recognition_failed"
FLAG_TEMPLATE_MISMATCH = "template_mismatch"

TEMPLATE_STATE_NOT_CHARGESHEET = "not_chargesheet"

# A page carrying all of these flags produced nothing usable — db.persist_page_v2
# uses this to mark the child attachment's status 'E' (errored) instead of 'G'.
ERRORED_CHILD_FLAGS = frozenset({
    FLAG_NOT_CHARGESHEET,
    FLAG_SKIPPED_NO_EXTRACTION,
    FLAG_BLANK_HEADER,
})
