# Review: `chargesheet_v3_4_material_overlap` (delivered 2026-08-21)

## Executive summary

This is not an incremental patch like every prior drop (v2 → v2.2.1 → v2.2.1.2)
— it's a genuine architectural rewrite. The vendor's own framing: v2.x is
**row-centric** (score all 206 locked cells, invent tie-break rules when one
physical circle scores on several rows); v3 is **mark-centric** (find the
physical marks on the page first, then ask which locked rows each mark
plausibly refers to). Geometry becomes diagnostic-only; a vision model looks
at an actual cropped image of each mark and returns integer indices into a
locked candidate list — never a numeric score decides ownership.

**This directly targets both issues currently open with the vendor**, and by
design should resolve them structurally rather than by threshold-tuning:

1. **The original adjacent-overlap bug** (`99395`/`36415`, the one that started
   this whole thread) — v2.x's row-scoring couldn't tell two independently
   "confirmed" adjacent rows apart. v3 has no such concept; each mark is
   adjudicated once, by a model looking at a picture, with an explicit
   ≥50% physical-code-box-coverage rule for enclosing neighboring rows.
2. **The cross-machine OpenCV geometry-score drift** (our `90460` saga —
   `0.386` on macOS ARM64 vs `0.5059` on the vendor's Windows workstation) —
   v3 has no confirmation threshold on a raw geometry score at all. The
   vendor's README explicitly calls this out: *"v3 has no confirmation
   threshold on a raw geometry score, so OpenCV build differences can no
   longer move a confirm/reject boundary."* I verified this structurally:
   geometry (`_geometry_hint`) is attached to marks purely for diagnostic
   telemetry and is never read by `reconcile()`'s decision logic.

However, **testing this delivery against its own included sample data
surfaced two real, concrete bugs** that should be fixed before integration —
detailed in §3. This is genuinely promising architecture with a couple of
shipping defects, not something to swap in as-is.

---

## 1. What's new — architecture

| | v2.x (row-centric, currently active) | v3.4 (mark-centric) |
|---|---|---|
| Unit of work | 206 locked code cells, each scored independently | 1–4 discrete physical marks per page |
| How ownership is decided | Geometry ring/arc score vs. per-cell thresholds; AI (`mark_resolver.py`) only reviews *already-ambiguous* groups | AI looks at a real cropped image of the mark and returns index selections; geometry never decides |
| Blank/noise region | Can accumulate a spurious score (registration halo) | Produces no mark at all — structurally can't generate a code |
| One loop spanning two rows | Needs explicit tie-break rules (`candidate_winner_margin`, `office_only_selection`, the whole `circle_multiple`/"distinct-multicircle" patch history) | One mark, one crop, one model decision — no special-case rules |
| Cross-machine reproducibility | A raw geometry score (`black_confirm_min`, etc.) gates confirm/reject — provably OpenCV-build-sensitive (our own `90460` investigation) | No geometry score gates confirm/reject; decision is visual, not numeric |
| Handwriting isolation | Subtract aligned reference from page, score residual directly | New: `handwriting.py` — keep a residual component only if it clears a *distance-from-nearest-printed-ink* gate (2.5px default). Explicitly targets registration-halo false positives (the `G0477`/`95004X80` cases mentioned in the vendor's own doc) |
| Candidate universe per mark | N/A (per-cell) | Ranked by physical proximity of observed ink to each row's glyph — no longer capped at 8 rows (the v3.3 defect that could drop a real code like `J3301` in a dense region) |

New modules: `handwriting.py`, `mark_localizer.py`, `mark_adjudicator.py`,
`page_audit.py` (full-page recall sweep, coordinates-only, feeds back into the
same crop-adjudication path). Reused unchanged: `alignment.py` (v2's ORB+ECC
registration, extracted as-is), `header_extract.py`, `dates.py`,
`template_registry.py`'s fail-closed manifest/checksum verification,
`model_client.py`/`auth.py` credential chain. `mark_resolver.py` and the
locked-template ring-scoring engine (`locked_template.py`) have **no
equivalent** — nothing scores rows anymore.

Package is **standalone** (doesn't import from `chargesheet_extraction_v2`),
same env var names carried over for compatibility.

---

## 2. Output schema changes (relevant to our integration)

Per-page output keeps `circled_procedures`, `circled_diagnoses`,
`manual_review_procedures`, `manual_review_diagnoses`, `procedure_codes`,
`procedure_summary`, `header`, `notes`, `flags`, `template_match`,
`orientation` — largely compatible with what `db.persist_page_v2`/`api.py`
already expect.

New/changed keys our integration would need to account for:
- `marks[]` (every localized mark + its full decision) and `rejected_marks[]`
  (marks the reader found nothing confirmable in) — replace v2.2.1.2's
  `possible_marks`/`suppressed_marks` concept, but with a **different, less
  detailed shape** (see bug §3.2).
- `handwriting_layer` (stats from the new isolation step) and `localizer`
  (mark counts by band) — new diagnostic blocks, no v2 equivalent.
- `page_images: {source, aligned}` — v3 persists both PNGs to disk per page
  as a first-class output, not just a debug option. Our `api.py`/`storage.py`
  already upload a page image via `on_page`; this would need reconciling
  (two different "the page image" concepts) rather than duplicating uploads.
- `page_audit: {...}` — recall-sweep stats, no v2 equivalent.
- No `circle_detection`/`mark_ai` block — the entire geometry-score/AI-summary
  telemetry shape from v2.2.1.2 is gone, replaced by `geometry_hint` (per-mark,
  diagnostic only) and `physical_coverage`/`why`/`resolution` fields on each
  confirmed item.

There is currently **no `pipeline_adapter.py`** (the wrapper we've written for
every prior drop to expose `process_pdf(pdf_path, client, registry, *,
pages_dir, want, auto_build, on_page) -> (results, metrics)`). `run_v3.py` is
a CLI script, not a library entry point with an `on_page` hook — integrating
this would mean writing that adapter from scratch (doable, same pattern as
before, just noting it isn't a smaller lift than prior integrations).

---

## 3. Bugs found — verified against the vendor's own included sample data

The package ships two real test PDFs with pre-computed output
(`july02-v32.json`, `june30-v24.json`, spanning 10 real pages) and a rendered
page-image cache. I ran both existing outputs and the code itself against
these to check the vendor's own claims.

### 3.1 `manual_review_procedures`/`manual_review_diagnoses` are dead code — always empty

The README and REQUIREMENTS.md both explicitly document this behavior:
*"only incomplete/faint/unclear circle-like evidence → manual_review"* and
*"Every localized mark ends as confirmed, manual_review, or rejected_marks."*

This does not happen. In `mark_adjudicator.py:339`, `reconcile()` hardcodes:
```python
"manual_review": [],
```
There is no code path anywhere that ever populates this list — grepped the
entire package. `run_v3.py` faithfully reads `r["manual_review"]` per mark
(line 260), but since it's always `[]`, the `seen_review` dict `run_v3.py`
builds is always empty, so `manual_review_procedures`/`manual_review_diagnoses`
are **empty on every page, unconditionally**.

Verified empirically, not just by reading code — across **all 10 pages** in
both included sample outputs:
```
manual_review_procs: 0   manual_review_dx: 0   (every single page, both files)
```
Meanwhile `rejected_marks` has real, substantial counts on the same pages
(up to 13 on one page) — confirming genuinely ambiguous/incomplete marks
*are* occurring, they're just landing in the wrong (and much less useful)
bucket. See §3.2 for why that matters.

The vendor's own test suite (`tests/test_v3_contract.py`, 11 tests, all pass)
has **zero test coverage of this path** — every test calls `reconcile()`
directly and only asserts on `r["confirmed"]`; none asserts anything about
`r["manual_review"]` ever containing an item. This is exactly the kind of gap
a single assertion would have caught.

### 3.2 Even if fixed, `rejected_marks` currently carries no candidate/code information

This compounds §3.1: a `rejected_marks` entry only contains
`mark_id, bbox, stroke_px, max_clearance_px, reason, other_marks` — no
candidate codes, no `physical_coverage` estimates, nothing a human reviewer
could use to pick a code. Confirmed by inspecting real entries in the sample
data, e.g.:
```json
{"mark_id": 16, "bbox": [0,1181,115,1225], "stroke_px": 0,
 "max_clearance_px": 0.0, "reason": "reader_found_no_selection", "other_marks": []}
```
Compare to v2.2.1.2's `manual_review_procedures` entries, which always carry
`code`, `description`, `section`, `deterministic_candidate_score`,
`ai_classification`, `ai_suggested` — enough for a reviewer to actually act.
So this isn't just "the bucket is empty" — the *information needed to build
the bucket correctly* isn't being carried through to where it would need to
be recorded. This is a real gap to raise with the vendor, not a one-line fix
on our side.

### 3.3 Shipped default threshold doesn't match documentation *or the vendor's own demo output*

| Source | `min_physical_coverage` default |
|---|---|
| `settings.py` (what actually runs) | **`0.70`** |
| `README_V3.md` | `0.50` |
| `REQUIREMENTS.md` | `0.50` |
| `mark_adjudicator.py`'s own module constant | `0.50` |
| Value recorded inside the vendor's own sample JSON output (`physical_coverage_threshold`) | `0.50` |

I confirmed this isn't cosmetic: scanning both included sample outputs for
confirmed codes with `physical_coverage` between 0.50 and 0.70 (i.e., codes
that were confirmed under the documented/demoed `0.50` threshold but would
**not** clear the actually-shipped `0.70` default) found **7 real confirmed
codes across both files** — including `99215`, `99205`, `81000`, `M70.7`,
`M79.1` — that would silently fail to confirm if this package were run
as-shipped, and (per §3.1/§3.2) would fall into an uninformative
`rejected_marks` entry instead of `manual_review`.

In other words: the demo output used to validate this delivery was not
generated with the settings this delivery actually ships.

### 3.4 `procedure_summary.invariant_ok` is now hardcoded `True`, always

v2.2.1.2 had `invariant_ok = bool(surfaced_procedure_codes)` — a real check,
and `procedure_code_missing_invariant` flag existed specifically to catch a
matched chargesheet that suspiciously surfaced zero procedures. v3.4's
`run_v3.py` sets `"invariant_ok": True` unconditionally, with a comment
explaining this is intentional philosophy ("v3's invariant is accounting, not
'a procedure must exist'... we never manufacture a procedure to avoid 0").
That's a defensible design choice, but combined with §3.1's dead
`manual_review` path, it does mean **there is currently no signal anywhere in
v3.4's output that would flag "this page probably has procedures we're
missing."** Worth deciding deliberately, not inheriting by accident.

### 3.5 Minor / hygiene

- No `CHANGELOG_V3_4.md` despite being referenced at the top of
  `README_V3.md` — same recurring documentation gap as prior deliveries.
- No env-var override hook for `alignment.py`'s `match_min` (hardcoded
  `0.78` default) — unlike v2.x's `settings.py`, there's no
  `CHARGESHEET_MATCH_MIN`-style mechanism in this package at all. Only
  matters if we ever need to retune page-match sensitivity.
- Package includes real PHI test fixtures that must be excluded before any
  integration (same pattern as every prior drop, just larger this time):
  `june-30.pdf`, `July-02.pdf`, `july02-v32.json`, `june30-v24.json`, the
  `july02-v32_pages/`/`june30-v24_pages/` rendered PNG directories (real
  patient pages), `runtime/header_cache_v3/*.json` (cached real header
  extractions with real patient names/DOBs), plus `__pycache__/` and
  `.DS_Store`.
- `opencv-python-headless>=4.9,<5` pin matches what we already standardized
  on after the cross-machine investigation — no change needed there.

---

## 4. Recommendation

**Don't integrate as-is.** The architecture is a real, well-reasoned fix for
both problems we've been chasing with the vendor, and the localizer/adjudicator
design holds up under direct code reading and testing against their own
sample data. But §3.1–3.3 are concrete, reproducible defects — not
theoretical — verified against the vendor's own bundled proof of correct
behavior. Shipping this as our active pipeline today would mean: no manual
review bucket ever populates (silently discarding the exact class of
ambiguous-but-real evidence this rewrite exists to handle safely), and the
default threshold would reject codes that even the vendor's own demo run
needed a looser setting to confirm.

Suggested next step: send the vendor §3.1–3.3 specifically (they're small,
precise, and each has a concrete repro from their own files) and ask for a
corrected drop before we invest in writing the `pipeline_adapter.py` /
`db.py` schema integration work for v3.
