# Charge-Sheet Output & Display Guide

This document explains how to consume the JSON produced by the current charge-sheet extraction pipeline and, most importantly, how to distinguish **confirmed/concrete output** from **manual-review output**.

The extraction pipeline uses a locked code catalog. The model cannot invent CPT/HCPCS/ICD codes outside that catalog.

---

## 1. The rule to remember

There are three practical levels of output:

| Level | JSON location | Meaning | Normal UI |
|---|---|---|---|
| **Confirmed / concrete** | `circled_procedures`, `circled_diagnoses` | The pipeline considers the physical mark sufficiently resolved to assign that exact locked code. | **Display as confirmed** |
| **Manual review** | `manual_review_procedures`, `manual_review_diagnoses` | There is meaningful mark evidence, but the exact code is intentionally not auto-confirmed. | **Display clearly as Needs Review** |
| **Diagnostic / audit only** | `possible_marks`, `suppressed_marks`, geometry internals | Candidate evidence used by the extractor and reviewer logic. It is not a billing selection. | **Hide in normal UI** |

### Critical safety rule

**Never treat `possible_marks` as selected codes.**

Also, **never treat every item in `procedure_codes` as confirmed**. `procedure_codes` is a convenience array that combines confirmed and manual-review procedures. Always check the item-level `status`.

---

## 2. Recommended page display

For each page, the normal application should show these areas:

1. **Patient / visit header**
2. **Confirmed procedures**
3. **Procedures needing manual review**
4. **Confirmed diagnoses**
5. **Diagnoses needing manual review**
6. **Extracted notes**
7. Optional reviewer/admin diagnostics

A simple display could look like:

```text
Patient
-------
Name: Jeffery Church
DOB: 01-20-1997
DOS: 06-30-2026
Insurance: Cigna

Confirmed Procedures
--------------------
99395  Wellness Established 18-39

Needs Manual Review
-------------------
Procedure:
36415  Routine Venipuncture
Reason: Adjacent code overlap
AI suggested: Yes

Diagnosis:
F11.20  Opioid Dependence
Reason: Adjacent code overlap
AI suggested: No

Confirmed Diagnoses
-------------------
None

Notes
-----
Jeffey Church
```

The **confirmed** and **manual review** sections should never be visually merged without a status indicator.

---

## 3. Page validity comes first

Before displaying extracted codes, check:

```json
"template_ok": true
```

and, when present:

```json
"recognition": {
  "is_chargesheet": true
}
```

### If `template_ok` is `false`

The page failed the locked-template match and the pipeline intentionally fails closed.

Expected behavior:

- do not display procedure or diagnosis codes as extracted selections;
- route the page to an exception state;
- show an operational message such as **Template mismatch / extraction skipped**.

A mismatch page can legitimately have:

```json
"procedure_summary": {
  "confirmed_count": 0,
  "manual_review_count": 0,
  "surfaced_count": 0,
  "invariant_ok": false
}
```

That is different from a valid charge sheet whose procedure mark needs review.

---

# 4. Concrete / confirmed output

## `circled_procedures`

This is the authoritative array of **confirmed procedure selections**.

Example:

```json
"circled_procedures": [
  {
    "code": "99214",
    "description": "Office visit, L4 Estb",
    "section": "Office Services",
    "mark": "circle",
    "confidence": 0.92,
    "detection": "residual_geometry",
    "geometry_mode": "full_loop"
  }
]
```

### How to use it

For normal application display:

```text
99214 — Office visit, L4 Estb
Status: Confirmed
```

These are the codes the extraction pipeline has resolved as actual selected procedure rows.

---

## `circled_diagnoses`

This is the authoritative array of **confirmed diagnosis selections**.

Example shape:

```json
"circled_diagnoses": [
  {
    "code": "M79.1",
    "description": "Myalgia",
    "section": "Musculoskeletal",
    "mark": "circle",
    "confidence": 0.77,
    "detection": "color_geometry",
    "geometry_mode": "partial_arc"
  }
]
```

### How to use it

Display these as:

```text
M79.1 — Myalgia
Status: Confirmed
```

The current policy is deliberately conservative for diagnoses. Residual black partial-arc diagnosis detections are not automatically treated as confirmed diagnoses.

---

# 5. Manual-review output

Manual review means:

> The pipeline has surfaced one or more locked codes because there is meaningful physical mark evidence, but the code assignment should be decided by a person.

Manual-review codes are **not confirmed selections**.

---

## `manual_review_procedures`

Example:

```json
"manual_review_procedures": [
  {
    "code": "36415",
    "description": "Routine Venipuncture",
    "section": "Miscellaneous",
    "status": "manual_review",
    "reason": "adjacent_code_overlap",
    "deterministic_candidate_score": 0.5335,
    "ai_classification": "circle_single",
    "ai_confidence": 0.85,
    "ai_suggested": true
  }
]
```

### Display recommendation

```text
36415 — Routine Venipuncture
NEEDS REVIEW
Reason: Adjacent code overlap
AI suggested this candidate
```

Do **not** display it using the same visual treatment as a confirmed procedure.

---

## `manual_review_diagnoses`

Same concept, but for diagnosis codes.

Example:

```json
"manual_review_diagnoses": [
  {
    "code": "F11.20",
    "description": "Opioid Dependence",
    "section": "Miscellaneous",
    "status": "manual_review",
    "reason": "adjacent_code_overlap",
    "deterministic_candidate_score": 0.5867,
    "ai_classification": "circle_single",
    "ai_confidence": 0.85,
    "ai_suggested": false
  }
]
```

### Important

`ai_suggested: false` does **not** mean the code should be discarded. It remains part of the manual-review group because the physical mark overlaps nearby locked rows.

---

# 6. Manual-review reasons

The current output uses two important procedure-review reasons.

## `adjacent_code_overlap`

Example:

```json
"reason": "adjacent_code_overlap"
```

Meaning:

- the detector found meaningful mark evidence in an area containing nearby code rows;
- when the region is one physical circle whose row ownership is unclear, the candidates remain manual review;
- when the visual reviewer identifies **multiple distinct circles** and an individual procedure row also has strong deterministic evidence, that strongly supported row may be confirmed independently;
- weaker neighboring/spillover rows remain reviewable or diagnostic rather than being promoted with the strong circle.

Therefore, `adjacent_code_overlap` describes the candidate region; it does **not** automatically mean every code in that region must remain manual. The authoritative final status is still the output bucket (`circled_procedures` vs `manual_review_procedures`).

---

## `procedure_required_manual_review`

Example:

```json
"reason": "procedure_required_manual_review"
```

Meaning:

- the page is a valid charge sheet;
- no procedure was safe enough to place in `circled_procedures`;
- returning a silent zero-procedure result is not acceptable for this business process;
- therefore the strongest valid procedure candidate group is surfaced as **manual review**.

This is not an auto-confirmation.

The reviewer must still select the correct procedure.

---

# 7. `procedure_codes` — convenient, but easy to misuse

`procedure_codes` is a merged consumer-friendly array.

It contains:

```text
circled_procedures
+
manual_review_procedures
```

A confirmed item is copied into this array with:

```json
"status": "confirmed"
```

A review item has:

```json
"status": "manual_review"
```

Example:

```json
"procedure_codes": [
  {
    "code": "99395",
    "description": "Wellness Established 18-39",
    "status": "confirmed"
  },
  {
    "code": "36415",
    "description": "Routine Venipuncture",
    "status": "manual_review",
    "reason": "adjacent_code_overlap"
  }
]
```

## Correct consumer logic

```javascript
const confirmedProcedures =
  page.procedure_codes.filter(x => x.status === "confirmed");

const reviewProcedures =
  page.procedure_codes.filter(x => x.status === "manual_review");
```

## Incorrect consumer logic

Do **not** do this:

```javascript
// WRONG: this makes manual-review codes look confirmed.
const selectedCodes = page.procedure_codes.map(x => x.code);
```

unless the next step explicitly preserves and respects the status.

---

# 8. `procedure_summary`

Every valid page includes a summary:

```json
"procedure_summary": {
  "confirmed_count": 1,
  "manual_review_count": 1,
  "surfaced_count": 2,
  "invariant_ok": true
}
```

### Meaning

| Field | Meaning |
|---|---|
| `confirmed_count` | Number of entries in `circled_procedures` |
| `manual_review_count` | Number of entries in `manual_review_procedures` |
| `surfaced_count` | Unique procedure codes surfaced as either confirmed or manual review |
| `invariant_ok` | At least one procedure code was surfaced |

### Business rule

For a valid recognized charge sheet, a silent procedure count of zero is not considered acceptable.

If nothing can be safely confirmed, a procedure candidate is surfaced for manual review instead.

If:

```json
"invariant_ok": false
```

the page should be routed as an **exception**, not treated as a successfully extracted page with no procedures.

---

# 9. `possible_marks`

Example:

```json
"possible_marks": [
  {
    "type": "possible_circle",
    "reason": "partial_circle_low_confidence",
    "kind": "diagnosis",
    "candidate_codes": [
      {
        "code": "R56.9",
        "score": 0.5287,
        "source": "residual_geometry"
      }
    ]
  }
]
```

## What it means

`possible_marks` contains detector evidence that is intentionally **not part of the primary coding result**.

It can include:

- low-confidence partial circles;
- scribbled regions;
- unresolved neighboring rows;
- weak residual geometry;
- mark groups retained for debugging/audit.

### Normal application behavior

**Ignore/hide `possible_marks`.**

Do not:

- bill from it;
- populate selected procedure/diagnosis fields from it;
- count it as a confirmed code;
- show every possible mark to a normal end user.

### Reviewer/debug behavior

It may be exposed in an expandable **Extraction Diagnostics** section for technical reviewers.

Some manual-review items are derived from evidence that also remains in `possible_marks`; therefore showing both in the normal UI can create confusing duplicates.

---

# 10. `suppressed_marks`

`suppressed_marks` is an internal/audit bucket for candidate marks that were suppressed by review logic.

Normal behavior:

```text
Do not display.
Do not bill.
Do not treat as selected.
```

It is useful only when debugging why a candidate disappeared from the primary output.

---

# 11. AI fields inside manual review

Manual-review objects can include:

```json
"ai_classification": "circle_single",
"ai_confidence": 0.85,
"ai_suggested": true
```

These fields are **review assistance**, not final coding status.

## `ai_classification`

Typical values can include:

- `circle_single`
- `circle_multiple`
- `ambiguous`
- `scribble`

The exact classification describes what the model saw physically in the candidate region.

## `ai_suggested`

```json
"ai_suggested": true
```

means the AI selected that existing locked candidate within the reviewed group.

It does **not** override:

```json
"status": "manual_review"
```

### UI recommendation

For a reviewer-facing screen, display:

```text
AI suggestion: 36415
```

as secondary information.

Do not display:

```text
Confirmed by AI
```

because that is not what the output means.

---

# 12. Scores and confidence values

Several fields contain numbers such as:

- `confidence`
- `deterministic_candidate_score`
- `ai_confidence`
- geometry scores under `circle_detection`

These are useful for diagnostics and tuning.

They should **not** be presented as calibrated medical/billing probabilities.

For normal users, the safest display is categorical:

```text
Confirmed
Needs Review
Exception
```

For an advanced reviewer/admin screen, raw scores can be shown as supporting diagnostic information.

---

# 13. Header fields

Example:

```json
"header": {
  "date": "06-30-2026",
  "name": "Jeffery Church",
  "dob": "01-20-1997",
  "prn": "CT 475139",
  "insurance": "Cigna",
  "copay": "",
  "amount_paid": "",
  "payment_type": ""
}
```

These fields come from the header/notes extraction path and are separate from the locked code-selection logic.

A header may occasionally be:

```json
"header": {}
```

Therefore the consumer should tolerate missing values.

Recommended display:

```javascript
const name = page.header?.name ?? "";
const dob = page.header?.dob ?? "";
```

Do not fail the entire page renderer because an optional header field is blank.

---

# 14. Notes

Example:

```json
"notes": [
  {
    "text": "Jeffey Church",
    "near": "bottom of page",
    "confidence": 0.6
  }
]
```

`notes` are transcribed handwritten/free-text content.

Recommended display:

```text
Extracted Notes
---------------
Jeffey Church
```

If desired, `near` and `confidence` can be shown only in a reviewer/debug view.

Notes are not procedure or diagnosis selections.

---

# 15. Flags

`flags` summarizes noteworthy extraction conditions.

Examples include:

```text
manual_code_review_required
mark_review_required
ambiguous_circle_assignment
partial_circle_low_confidence
scribbled_region_review
locked_template_mismatch
procedure_code_missing_invariant
```

## Recommended consumer use

### High-value operational flags

`manual_code_review_required`

> Put the page in the human review queue.

`locked_template_mismatch`

> Treat as extraction exception.

`procedure_code_missing_invariant`

> Treat as extraction exception; a valid page did not surface a procedure.

### Diagnostic flags

Flags such as:

```text
partial_circle_low_confidence
scribbled_region_review
ambiguous_circle_assignment
```

are useful for reviewer/admin context but do not by themselves determine billing status.

The authoritative status remains the output bucket:

```text
circled_*          = confirmed
manual_review_*    = review
possible_marks     = diagnostic
```

---

# 16. Internal diagnostic objects

The following fields are primarily for engineering, audit, and troubleshooting:

```text
mark_ai
circle_detection
template_match
recognition
orientation
header_notes_source
page_sha256
```

They help answer questions such as:

- Was the page aligned to the locked template?
- Did AI review a mark candidate?
- Was a result promoted by AI visual review?
- Was black residual geometry enabled?
- Was a header result read from cache?
- What was the template match score?

They are not required for the basic billing-code display.

---

# 17. Top-level output

The root JSON includes information about the entire source PDF.

Example:

```json
{
  "source_pdf": "june-30.pdf",
  "page_count": 23,
  "pipeline": "locked-candidates-ai-verified-v2.2.1-manual-proc-review",
  "template_id": "nwa_internal_medicine_superbill_locked_v1",
  "template_version": 1,
  "safety": {
    "code_universe": "locked_catalog_candidates_only",
    "ai_can_invent_codes": false,
    "ambiguous_candidates_are_preserved": true,
    "manual_review_codes_are_separate_from_confirmed": true,
    "procedure_zero_is_not_a_valid_silent_result": true
  },
  "pages": []
}
```

The application will normally iterate:

```javascript
for (const page of output.pages) {
  // render one charge sheet / patient page
}
```

---

# 18. Recommended front-end mapping

A safe normalized view model can be created like this:

```javascript
function mapChargeSheetPage(page) {
  const valid =
    page.template_ok === true &&
    page.recognition?.is_chargesheet !== false;

  if (!valid) {
    return {
      page: page.page,
      state: "exception",
      reason: "template_mismatch",
      header: page.header ?? {}
    };
  }

  const confirmedProcedures = page.circled_procedures ?? [];
  const reviewProcedures = page.manual_review_procedures ?? [];

  const confirmedDiagnoses = page.circled_diagnoses ?? [];
  const reviewDiagnoses = page.manual_review_diagnoses ?? [];

  return {
    page: page.page,
    state:
      reviewProcedures.length || reviewDiagnoses.length
        ? "needs_review"
        : "complete",

    header: page.header ?? {},

    confirmedProcedures,
    reviewProcedures,

    confirmedDiagnoses,
    reviewDiagnoses,

    notes: page.notes ?? [],

    procedureSummary: page.procedure_summary ?? {}
  };
}
```

This intentionally does not use `possible_marks` for the normal display.

---

# 19. Recommended visual states

Use three unmistakable states.

## Confirmed

Label:

```text
CONFIRMED
```

Source:

```text
circled_procedures
circled_diagnoses
```

A reviewer should understand that no action is required solely because of this code.

---

## Needs Review

Label:

```text
NEEDS REVIEW
```

Source:

```text
manual_review_procedures
manual_review_diagnoses
```

Recommended actions:

```text
Accept candidate
Choose alternate candidate
Correct selection
```

The application should preserve the reviewer decision separately rather than silently rewriting the raw extraction JSON.

---

## Exception

Label:

```text
EXTRACTION EXCEPTION
```

Examples:

```text
template_ok = false
procedure_summary.invariant_ok = false
procedure_code_missing_invariant flag
```

This is not the same thing as ordinary manual code review.

---

# 20. Page 1 example from the current regression output

The current regression output demonstrates all of the important concepts on one page.

### Confirmed procedure

```text
99395 — Wellness Established 18-39
```

It appears in:

```text
circled_procedures
```

and in:

```text
procedure_codes
status = confirmed
```

### Manual-review procedure

```text
36415 — Routine Venipuncture
```

It appears in:

```text
manual_review_procedures
```

and in:

```text
procedure_codes
status = manual_review
```

### Manual-review diagnosis

```text
F11.20 — Opioid Dependence
```

It appears in:

```text
manual_review_diagnoses
```

It is **not** a confirmed diagnosis.

### Confirmed diagnoses

```text
None
```

Therefore:

```json
"circled_diagnoses": []
```

is correct for that page.

### Diagnostic mark evidence

Additional low-confidence and scribbled items remain in:

```text
possible_marks
```

They should be hidden from the normal application display.

---

# 21. Consumer decision matrix

| JSON field | Concrete? | Requires human review? | Display normally? | Can feed confirmed billing selection automatically? |
|---|---:|---:|---:|---:|
| `circled_procedures` | **Yes** | No | **Yes** | **Yes** |
| `circled_diagnoses` | **Yes** | No | **Yes** | **Yes** |
| `manual_review_procedures` | No | **Yes** | **Yes, as review** | **No** |
| `manual_review_diagnoses` | No | **Yes** | **Yes, as review** | **No** |
| `procedure_codes` item with `status=confirmed` | **Yes** | No | Yes | **Yes** |
| `procedure_codes` item with `status=manual_review` | No | **Yes** | Yes, as review | **No** |
| `possible_marks` | No | Not directly; diagnostic | No | **No** |
| `suppressed_marks` | No | No; audit/debug | No | **No** |
| `notes` | N/A | Optional transcription review | Yes | No |
| `flags` | N/A | Can route workflow | Usually not directly | No |

---

# 22. Rules for downstream implementation

## Do

- Use `circled_procedures` for confirmed procedures.
- Use `circled_diagnoses` for confirmed diagnoses.
- Send `manual_review_procedures` and `manual_review_diagnoses` to the human-review workflow.
- If consuming `procedure_codes`, always branch on `status`.
- Check `template_ok`.
- Check `procedure_summary.invariant_ok` on valid charge sheets.
- Keep confirmed and review items visually separate.
- Preserve the raw JSON for audit/debug.

## Do not

- Do not use `possible_marks` as selected codes.
- Do not send manual-review codes to billing as confirmed.
- Do not assume `ai_suggested=true` means confirmed.
- Do not interpret raw detector scores as calibrated probabilities.
- Do not hide a manual-review procedure just because `circled_procedures` is empty.
- Do not interpret a template mismatch as a genuine zero-procedure charge sheet.

---

# 23. Simplest safe consumer contract

If another team wants only the minimum required output, use these fields:

```json
{
  "page": 1,
  "template_ok": true,
  "header": {},
  "circled_procedures": [],
  "manual_review_procedures": [],
  "circled_diagnoses": [],
  "manual_review_diagnoses": [],
  "procedure_summary": {},
  "notes": [],
  "flags": []
}
```

Everything else can remain available in the raw result for diagnostics.

The status contract is:

```text
circled_*       → CONCRETE / CONFIRMED
manual_review_* → HUMAN REVIEW REQUIRED
possible_marks  → IGNORE IN NORMAL DISPLAY
suppressed_marks→ DEBUG/AUDIT ONLY
```

That distinction should be preserved through every downstream API, database table, and user interface.
