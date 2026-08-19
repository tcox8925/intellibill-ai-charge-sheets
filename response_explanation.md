# Extraction result — field-by-field explanation

This describes the JSON object produced **per page** by the active CV pipeline
(`v2_2_1_computer_vision/pipeline_adapter.py`, function `_matched_result()` /
`_mismatch_result()`). This is the `raw_extracted_data` stored on each child
attachment row, and one element of the `results` list returned by
`process_pdf()`.

Worked example below is the actual page you asked about (`june-26-page-19.png`,
patient Arturo Perez).

---

## Top level

| Key | Meaning |
|---|---|
| `page` | 1-based page number within the source PDF. |
| `page_sha256` | SHA-256 of the rendered page image. Used as a cache key (header/notes cache, mark-AI cache) and for de-duplication. |
| `template_ok` | `true` if the page matched the locked chargesheet template well enough to run detection at all. `false` → page was skipped as not-a-chargesheet (see `_mismatch_result`), and most of the fields below won't be present. |
| `header` | Patient/visit header fields OCR'd by the AI header-notes call. See below. |
| `notes` | Free-text handwritten notes the AI spotted outside the header (e.g. lab values). See below. |
| `flags` | Machine-readable list of everything notable about this page. See the **Flags glossary** section — this is the most important field for triage. |
| `circled_procedures` | Procedure (CPT/HCPCS) codes the pipeline is confident were circled — i.e. **auto-confirmed**, no human needs to look at these. |
| `circled_diagnoses` | Same, but ICD diagnosis codes. |
| `manual_review_procedures` | Procedure codes with real mark evidence, but the pipeline could **not** safely auto-confirm which exact code was circled (or the AI reviewer wasn't confident/allowed to promote it). A human needs to pick the right one. |
| `manual_review_diagnoses` | Same, but diagnosis codes. |
| `procedure_codes` | Convenience list = `circled_procedures` (each tagged `"status": "confirmed"`) + `manual_review_procedures` (already tagged `"status": "manual_review"`). Use this if you just want "every procedure code this page could possibly mean," with a status flag telling you whether it's trustworthy. |
| `procedure_summary` | Roll-up counts over `procedure_codes` (see below). |
| `possible_marks` | Raw/audit view of every ambiguous or weak mark candidate the geometry detector found, before/after AI review. Kept for debugging; `manual_review_procedures/diagnoses` is the "already summarized for you" version of the useful parts of this. |
| `suppressed_marks` | Marks that were in `possible_marks` but the AI reviewer confidently decided were *not* a deliberate mark (e.g. table noise), and geometry evidence was too weak to keep them visible. Kept here instead of silently deleted, for audit. |
| `mark_ai` | Summary of what the AI visual mark-reviewer did on this page overall. See below. |
| `header_notes_source` | `"model"` = header/notes were freshly extracted this run; `"cache"` = reused from a prior run of the same page (same `page_sha256`). |
| `circle_detection` | Raw geometry/algorithm diagnostics for the whole page (thresholds, counts, alignment quality). Debug/tuning info, not needed for normal use. |
| `template_match` | How well this page matched the one locked chargesheet template. `score` is 0–1; `state: "known"` means matched. |
| `recognition` | Simplified duplicate of the match info: `is_chargesheet` + `confidence` (= `template_match.score`). |
| `orientation` | How the page was rotated to align with the template before detection. |

---

## `header`

OCR'd from the top of the page by the vision model. Self-explanatory fields:
`dob`, `prn` (patient record number), `date` (service date), `name`, `copay`,
`insurance`, `amount_paid`, `payment_type`. Dates are normalized to
`MM-DD-YYYY` (see `service_date_year_normalized` in the flags glossary — a
2-digit year like `26` was expanded to `2026`).

## `notes`

```json
{"near": "Labs section", "text": "AIC 6.2%", "confidence": 0.75}
```
- `near` — the section of the page the model saw this handwriting next to (positional hint only, not a code).
- `text` — the model's best-effort transcription of the handwritten note.
- `confidence` — the model's own confidence in that transcription (**this is unrelated to `CHARGESHEET_MATCH_MIN` / template match confidence** — it's purely "how sure am I I read this handwriting correctly").

## `mark_ai` (page-level summary)

```json
{"model": "claude-opus-4-6", "source": "model", "status": "ok",
 "resolver": "candidate_crop_resolver_v2_2_1_manual_proc_review",
 "promoted_codes": [], "promoted_count": 0, "suppressed_count": 0,
 "remaining_possible_count": 3}
```
- `status` — `ok` (AI review ran), `disabled` (mark AI turned off), `not_needed` (no ambiguous marks to review), or `failed` (AI call errored — see `mark_ai_review_failed` flag).
- `source` — `"model"` = called the AI this run; `"cache"` = reused a prior verdict for this exact page+candidate set.
- `promoted_codes` / `promoted_count` — codes the AI was confident enough about to auto-confirm (moved from `possible_marks` into `circled_procedures`/`circled_diagnoses`). Zero here means nothing on this page was safely auto-confirmable by AI.
- `suppressed_count` — marks the AI decided were not real marks at all (see `suppressed_marks`).
- `remaining_possible_count` — how many ambiguous marks are still sitting in `possible_marks` needing a human.

## `orientation`

```json
{"method": "locked_template_registration", "detected": true, "applied_rotation_deg": 90}
```
`applied_rotation_deg` is how many degrees the page image was rotated
(counter-clockwise) to match the reference template's orientation before any
detection ran. This is a real, physical correction — a value of `90` means
the source page was actually rotated 90° from upright in the PDF.

---

## `possible_marks[]` (raw candidate objects)

Each entry is one location on the page where the geometry detector found
*some* ink/circle evidence but couldn't cleanly resolve it into a confirmed
code by itself.

Common shape:
```json
{
  "kind": "procedure" | "diagnosis",
  "type": "possible_circle" | "ambiguous_circle",
  "reason": "partial_circle_low_confidence" | "adjacent_code_overlap" | "scribbled_region",
  "evidence": { ...geometry scores, see below... },
  "ai_review": { ...what the AI concluded, see below... },
  "review_region": {"x1":.., "y1":.., "x2":.., "y2":..},
  "candidate_codes": [{"code": "...", "score": 0.76, "source": "residual_geometry"}],
  "display_candidate_codes": [...],
  "winner_margin": 0.231,
  "manual_review": true,
  "manual_review_reason": "adjacent_code_overlap"
}
```

- **`reason` / `type`** — why this mark is ambiguous:
  - `partial_circle_low_confidence` — geometry found a partial ring around one code, but it's too weak/incomplete to trust on its own (didn't clear the confirm thresholds).
  - `adjacent_code_overlap` — a hand-drawn circle plausibly touches **two neighboring codes** in the table (e.g. `99214` and `99205` sit right next to each other), and the geometry can't tell which one the person meant to circle.
  - `scribbled_region` — looks like handwriting/strike-through rather than a clean circle.
- **`evidence`** — raw geometry measurements for the *best* candidate at this location (only present on `possible_circle` entries):
  - `score` — the combined weighted confidence score (0–1) from all the sub-measurements below. This is what gets compared against confirm thresholds.
  - `radius` — fitted ellipse radius in pixels `[rx, ry]`.
  - `center_offset` — how far the fitted circle's center is from the expected/locked cell center, in pixels `[dx, dy]`. Large offsets suggest the circle belongs to a neighboring row/cell instead.
  - `ring_coverage` — fraction of the expected ring (circle outline) that actually has ink on it.
  - `ring_capture` — fraction of nearby foreground ink that falls inside the expected ring band (vs. scattered elsewhere).
  - `sector_coverage` — how many of the 12 angular "clock" sectors around the ring have ink (0–12). Higher = more complete circle.
  - `longest_arc_sectors` — the longest *unbroken run* of inked sectors — distinguishes "a full loop" from "ink scattered around but never actually connected."
  - `side_coverage` — fraction of the 4 quadrants (top/bottom/left/right) that have some ink (0–1). A real circle should hit most/all sides; a stray mark on one side won't.
  - `interior_ink` — how much ink is *inside* the circle (vs. just on the ring). High interior ink can mean a scribble/fill-in rather than a clean circle, and it penalizes the score.
- **`ai_review`** — the constrained AI visual reviewer's verdict for this specific mark (see `mark_resolver.py`):
  - `status: "not_reviewed", reason: "geometry_below_ai_review_gate"` — this candidate's geometry score never even cleared the minimum bar to bother asking the AI (weak evidence stays weak; AI can't rescue it).
  - When actually reviewed: `classification` (`circle_single` / `circle_multiple` / `scribble` / `ambiguous` / `none`), `confidence` (AI's own confidence), `candidate_codes_selected` (which code(s) the AI thinks the circle belongs to — **the AI can only pick from codes that were already geometry candidates, it can never invent a new code**), `reason` (short plain-English visual justification, deliberately never contains the billing code text itself, per the resolver's safety design), `promotion_geometry_eligible` (`true` only if this got auto-confirmed into `circled_procedures`/`circled_diagnoses`; `false` = stayed manual-review even though the AI answered).
- **`winner_margin`** — for `adjacent_code_overlap` cases, how much more evidence the leading candidate has over the runner-up (0–1). Small margin (like `0.11`–`0.23` here) means the two candidates are genuinely close, reinforcing why it needs a human.
- **`manual_review` / `manual_review_reason`** — set to `true` when this mark definitely needs a human, even after AI review (e.g. the AI picked `99214` here, but because `99214`/`99205` aren't both in the "Office Services only" safety-allowed section pairing, or confidence/ownership wasn't clean enough, it was **not** auto-promoted — it's surfaced instead in `manual_review_procedures`).
- **`candidate_codes` / `display_candidate_codes`** — every code geometry thinks this mark could plausibly be, sorted by score; `display_candidate_codes` is a trimmed list meant for showing a human 1–2 best guesses instead of everything.

### Your example, decoded

1. **First mark** (`possible_circle`, `90669`, score `0.34`) — weak partial circle near a Labs-area code. Score `0.34` never reached the AI-review gate (`geometry_below_ai_review_gate`), so it just sits in `possible_marks` as low-confidence evidence — not promoted, not necessarily wrong, just unproven.
2. **Second mark** (`ambiguous_circle`, procedure, `99214` vs `99205`) — a single hand-drawn oval overlapping the boundary between the "Office visit, L4 Established" and "Office visit, L5 New" rows. The AI looked at the crop and judged the circle's center sits closer to `99214` (`classification: circle_single`, confidence `0.82`), but `promotion_geometry_eligible: false` — meaning even with a fairly confident AI read, the pipeline's safety rule didn't auto-confirm it (both codes are in the "Office Services" section, so it *is* eligible for AI promotion in principle — here it likely fell short on `winner_margin`/geometry-eligibility of the selected candidate rather than on the section-gate). Both `99214` and `99205` end up in `manual_review_procedures` for a human to pick between, with `99214` marked `ai_suggested: true` as the AI's best guess to speed that review up.
3. **Third mark** (`ambiguous_circle`, diagnosis, `Z13.89` vs `F31.9`) — same idea for two adjacent diagnosis codes, but this one's geometry score (`0.44`) never even cleared the AI-review gate, so it's purely a geometry-only ambiguous overlap sitting in `possible_marks`. (Note: it's *not* in `manual_review_diagnoses` in this payload — the manual-review-diagnosis bucket only got populated when there's a viable candidate group; check `manual_review_diagnoses` being `[]` here likely means this pair didn't clear the bar `_manual_review_code_buckets()` uses to surface a diagnosis group, so on this page no diagnosis manual-review is being asked for even though this ambiguity exists — worth knowing if you expected to see it there.)

---

## `procedure_codes[]` / `circled_procedures[]` / `manual_review_procedures[]` items

```json
{"code": "83036", "mark": "circle", "status": "confirmed", "section": "Labs",
 "detection": "residual_geometry", "confidence": 0.91, "description": "A1C",
 "geometry_mode": "full_loop"}
```
```json
{"code": "99214", "reason": "adjacent_code_overlap", "status": "manual_review",
 "section": "Office Services", "description": "Office visit, L4 Estb",
 "ai_suggested": true, "ai_confidence": 0.82, "ai_classification": "circle_single",
 "deterministic_candidate_score": 0.7608}
```
- `status` — `confirmed` (trust it, no review needed) vs `manual_review` (needs a human).
- `section` — the catalog section this code belongs to (`Labs`, `Office Services`, `Endocrine`, `Miscellaneous`, etc.) — this matters because the AI mark-resolver is only allowed to auto-promote ambiguous **Office Services** pairs; ambiguous pairs from other sections (e.g. a Miscellaneous/diagnosis pair) are *always* routed to manual review by design, regardless of AI confidence.
- `detection` — `residual_geometry` = plain geometric circle-fit found this; other values would indicate a different detection path (e.g. color-based).
- `confidence` (on confirmed items) — blended geometric+AI confidence when applicable; on manual_review items, see `ai_confidence`/`deterministic_candidate_score` instead.
- `geometry_mode` — `full_loop` (clean complete circle) vs `partial_arc` (an incomplete ring) vs `ai_verified_candidate` (confirmed via AI rather than geometry alone).
- On manual-review items: `ai_suggested` (`true` = this is the AI's pick among the tied candidates, shown to help a human triage faster — **not** an auto-confirmation), `ai_confidence`, `ai_classification`, `deterministic_candidate_score` (the raw geometry score before any AI involvement).

## `procedure_summary`

```json
{"invariant_ok": true, "surfaced_count": 3, "confirmed_count": 1, "manual_review_count": 2}
```
- `confirmed_count` / `manual_review_count` — sizes of `circled_procedures` / `manual_review_procedures`.
- `surfaced_count` — unique codes across both (here: `83036`, `99214`, `99205` = 3).
- `invariant_ok` — sanity check that the page surfaced *at least one* procedure code somewhere (confirmed or manual-review). `false` would mean the page matched the template but somehow produced zero procedure evidence at all — flagged separately as `procedure_code_missing_invariant`.

---

## Flags glossary

`flags` is a deduplicated bag of everything notable on the page. Relevant ones seen in your example:

| Flag | Meaning |
|---|---|
| `illegible_handwriting` | The AI couldn't confidently read some handwriting on the page (header or notes). |
| `service_date_year_normalized` | The service date's year was written as 2 digits (e.g. `26`) and was expanded to 4 digits (`2026`) automatically. |
| `locked_coordinate_candidate_source` | Informational: this page's candidates came from the locked-template fixed-coordinate detector (always present for a matched page — not a problem indicator). |
| `black_circle_geometry` | At least one code was confirmed via plain black-ink circle geometry (as opposed to color-pen detection, which is currently unused/`0` here). |
| `manual_code_review_required` | Top-level "heads up" flag — this page has at least one code in `manual_review_procedures`/`manual_review_diagnoses` that a human needs to resolve. |
| `mark_review_required` | There is at least one entry left in `possible_marks` needing eyes on it (broader than just codes — includes weak/undecided marks too). |
| `ambiguous_circle_assignment` | At least one `possible_marks` entry has `reason: adjacent_code_overlap` (a circle spans two neighboring codes). |
| `partial_circle_low_confidence` | At least one `possible_marks` entry is a weak/incomplete circle that didn't clear confirm thresholds. |
| `ai_visual_candidate_promoted` | *(not present here, since `promoted_count: 0`)* Would appear if the AI reviewer successfully auto-confirmed at least one code this page. |
| `procedure_code_missing_invariant` | *(not present here)* Would mean **zero** procedure codes were surfaced anywhere on a matched page — likely a real detection gap worth investigating. |
| `ai_suppressed_no_deliberate_mark` | *(not present here)* Would mean the AI confidently cleared out a weak geometry candidate as "not a real mark." |
| `mark_ai_review_failed` | *(not present here)* Would mean the AI mark-review call itself errored (e.g. connection/auth issue) — codes stay in their pre-AI state when this happens. |
| `header_notes_reused_from_cache` / `mark_ai_reused_from_cache` | *(not present here — this page's `source` was `"model"`, i.e. freshly computed, not cached)* |
| `scribbled_region_review` | Would appear if a mark was classified as handwriting/strike-through rather than a clean circle. |
| `blank_header` | Would appear if no patient name or DOB was extracted at all. |
| `locked_template_mismatch` / `not_chargesheet` / `skipped_no_extraction` | Page didn't match the chargesheet template well enough — `template_ok: false`, detection skipped entirely. |

---

## Quick mental model

1. **`circled_procedures` / `circled_diagnoses`** = done, trust it.
2. **`manual_review_procedures` / `manual_review_diagnoses`** = "we found real ink evidence pointing at 2+ possible codes (or the AI wasn't allowed/confident enough to pick one for you) — a human must choose."
3. **`possible_marks`** = the full audit trail behind #1 and #2, plus genuinely weak/unproven evidence that isn't strong enough to even ask a human about yet.
4. **`mark_ai`** = "did the AI helper run, and did it manage to resolve anything on its own."
5. **`flags`** = fast way to know, without reading the whole payload, whether this page needs attention (`manual_code_review_required`, `mark_review_required`, `mark_ai_review_failed`, `procedure_code_missing_invariant` are the ones to watch for).
