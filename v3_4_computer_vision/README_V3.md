# v3.4 CURRENT PHYSICAL-MARK POLICY

The current decision rule is physical evidence only, with a material-overlap gate:

- A real circle/arc must be visibly handwritten; template residue, registration halos, printed rules, ticks, and imagined continuations never confirm a code.
- Candidate generation is high-recall and no longer truncates dense J-code regions at eight rows.
- The crop reader returns an estimated `physical_coverage` for each locked code box affected by the mark.
- A neighboring row is included only when at least **50% of its printed code box** is physically enclosed/surrounded. A slight touch or boundary crossing is ignored.
- For an incomplete real arc, one visually dominant row may still confirm even when the arc is too broken to create a 50% closed enclosure; adjacent rows still require 50%.
- Geometry remains diagnostic only and never chooses, vetoes, downgrades, or tie-breaks a code.
- `procedure_codes` contains confirmed procedures only.
- Malformed crop-reader JSON is retried once before the mark is rejected.
- Rendered source pages and aligned/upright processing pages are persisted for every selected page, and orientation is returned per page plus as a top-level rotation distribution.

See `CHANGELOG_V3_4.md`.

---

# Charge-sheet extraction v3 — mark-centric, crop-adjudicated

## What was actually wrong

Nothing in v2.4 was badly implemented. The architecture asked a question that
cannot be answered reliably.

v2.x is **row-centric**: for each of the 206 locked code cells it searches a
neighbourhood of ellipse centres and radii for arc evidence, and scores that row.
Two consequences follow directly from that framing, and no amount of threshold
tuning removes either:

1. **One physical loop scores on several rows.** Row pitch on this template is
   29px; `ring_ry_values` reaches 25 and `ring_patch_half_height` is 45. A loop
   drawn around `99214` is inside the search window of `99205` and `99215` too.
   The pipeline then has to *invent* ownership — winner margins, adjacent-pair
   rules, multicircle exclusions. Each rule fixes one sheet and risks another.
   That is the Ramiro case, and it is structural.

2. **A blank cell can accumulate evidence out of nothing.** The residual is
   `page − aligned_reference`. Global ORB+ECC registration on a scanned, slightly
   warped page leaves 1–2px of local error, which leaves a thin sliver of
   residual hugging every printed stroke. The ring search then finds *some*
   ellipse among that residue. That is Robert's `G0477` at 0.4412 and Hope's
   `95004X80` at 0.4648 — and the history doc is right that they came from
   deterministic geometry, not from the model.

The doc's own conclusion is the correct one: *the remaining defect is upstream
evidence quality.* v3 acts on that instead of tuning downstream again.

## What v3 changes

Two changes. Everything else — locked catalog, registration, fail-closed
template policy, header/notes extraction — is reused unchanged.

### 1. Isolate handwriting with a clearance gate, not an arc score

`handwriting.py` binarises page and reference with the same operator, subtracts,
then applies one physical test to each residual component:

> **Maximum distance from the nearest printed pixel.**
>
> A misregistration halo lives entirely within ~1–2px of printed ink.
> A real pen stroke has a core several pixels clear of any printed ink.

Components that never clear `clearance_min` (default 2.5px at 200 DPI) are
dropped. This is not a quality score — it does not care whether the arc is
complete, so it cannot delete a legitimate partial arc the way a
"partial arc below 0.50 = false" rule would. On July-02 page 19 it removes 31
halo components (1,771px) and keeps every real stroke.

Result on the pages in question, with the printed layer removed:

```
page 6  residual 42,990px -> handwriting 41,684px, 0 halo components
page 19 residual 46,763px -> handwriting 41,856px, 31 halo components dropped
```

### 2. Find marks, then ask about each mark — don't score rows

`mark_localizer.py` groups the handwriting layer into **discrete mark objects**:
bridge pen lifts and the gaps torn where the loop crossed a printed rule
(`bridge_kernel`, 15px — measured gaps on real sheets run 5–16px), connected
components, merge fragments of the same loop, then attach the locked catalog
rows that fall inside the mark's neighbourhood.

This inverts the question:

| | v2.x | v3 |
|---|---|---|
| unit of work | 206 rows | 1–4 marks per page |
| blank region | can score | produces nothing |
| one loop over two rows | two competing scores | one mark, one decision |
| three separate loops | needs `circle_multiple` rules | three marks, independently |

`mark_localizer.py` also declares the template's **write-in regions** (patient
header, insurance/copay/payment block, bottom log strip). Cursive in those
blocks is classified `write_in` and structurally cannot reach the code path.
On page 6 that removes 8 of 9 marks before any model call.

`mark_adjudicator.py` then renders each mark's neighbourhood at 2.5× and asks
the reader a closed question: *which of these listed rows does the loop
enclose?* The crop looks like this:

```
 99214 | Office visit, L4 Estb
(99215)| Office visit, L5 Estb      <- loop grazes here
(99395)| Wellness Established 18-39 <- loop encloses here
 99396 | Wellness Established 40-64
```

At that resolution the answer is not a judgement call. This is the whole point:
v2.4 asked a model to arbitrate between two decimal scores on a full page; v3
shows it the same picture a human biller looks at.

### Safety contract — unchanged, and now enforced structurally

* Candidate lists are built by Python from `catalog.json` coordinates.
* The reader returns **integer indices into that list**. Never a code.
* Out-of-range indices are dropped and recorded in `invalid_indexes_rejected`.
* The full-page recall sweep (`page_audit.py`) returns **normalised coordinates
  only** — it is structurally incapable of naming a code. Anything it reports
  becomes a synthetic mark and goes through the same crop path.

So a code can still only enter the output by being a locked catalog row that
geometry placed inside a crop and the reader selected in that crop.

## Where geometry still lives

Geometry remains useful, but it is **diagnostic/corroborating only**. The localizer
may compute vertical/bracket hints for each candidate row and write them into debug
output. Those hints do **not** confirm, veto, downgrade, or tie-break a crop-reader
decision.

The decision policy is now physical-circle-first with an explicit material-overlap test:

1. establish that a real handwritten circle/arc physically exists;
2. estimate how much of each candidate's printed code box is enclosed/surrounded;
3. include a row when `physical_coverage >= 0.50`; a boundary touch does not count;
4. for an incomplete arc, a clearly dominant visual row may still be kept even if the arc is too broken to make a 50% closed enclosure;
5. if two rows each meet the 50% physical-coverage rule, confirm both;
6. no physical circle -> rejected mark with a reason;
7. geometry disagreement -> telemetry only.

This avoids the v3.3 overcapture where a circle centered on `99214` also confirmed
`99205` merely because the top stroke crossed that neighboring row.

## Against the acceptance criteria in the issue history

| Criterion | v3 |
|---|---|
| Ramiro: visible 99214 must not be overridden by neighboring geometry | A confident crop-reader selection of `99214` confirms even when diagnostic geometry ranks `99205` higher. |
| Richard: distinct multicircle intact | Two loops = two marks, or one crop in which the reader returns two indices. No `circle_multiple` special case exists to break. |
| Complete circle overlaps adjacent rows | Confirm each row whose printed code box has at least 50% physical coverage. Slight contact is ignored; two materially enclosed rows both confirm. |
| Robert/Hope blank residual artifacts never confirm | Halo never becomes a mark. Even if one survives, the crop reader returns empty selections and it lands in `rejected_marks` with its reason. |
| Legitimate faint/broken evidence stays surfaced | A real incomplete arc may confirm a clear dominant row; adjacent rows require the same 50% material-coverage rule. |
| No geometry score tuning | The new 0.50 gate is a physical code-box coverage rule, not a v2-style residual geometry score. |
| No code outside locked coordinates | Index-only response contract; coordinate-only audit. |
| "I can show both, but I cannot not show" | Every localized mark ends as confirmed, manual_review, or an entry in `rejected_marks` with a reason. Nothing is dropped silently. |

Also addressed: **issue 4, cross-machine score drift.** v3 has no confirmation
threshold on a raw geometry score, so OpenCV build differences can no longer
move a confirm/reject boundary.

## Files

This package is standalone — see REQUIREMENTS.md for the full file list, install
steps and the Poppler dependency. The four modules that carry the architecture:

```
handwriting.py       clearance-gated handwriting isolation + glyph boxes
mark_localizer.py    mark grouping, write-in regions, candidates, geometry hint, crops
mark_adjudicator.py  crop reader contract (index-only) + reconcile policy
page_audit.py        full-page recall sweep, coordinates-only
```

`alignment.py` is v2's registration (ORB + ECC + orientation pick) extracted
unchanged in behaviour. v2's ring/arc scoring, `mark_resolver.py` and
`visual_mark_reader.py` have no equivalent here — nothing scores rows.

## Authentication

The default internal credential flow is the same shared pattern used by the
existing EOB_v9/charge-sheet environment:

`az login` -> `DefaultAzureCredential` -> shared Azure Key Vault -> configured
Anthropic secret -> `AnthropicFoundry`.

No Anthropic secret value is stored in the package. An explicit
`ANTHROPIC_API_KEY` remains an optional deployment override, but local internal
testing should normally require only `az login`. Run `python preflight.py --with-ai`
to verify the complete credential path before a regression run.


## Running it

```bash
# see what the localizer found, no model calls, dumps every crop it would send
python run_v3.py July-02.pdf --pages 6,19,21 --no-ai --dump-crops ./crops

# full pipeline; source/aligned page PNGs persist automatically beside the output
python run_v3.py July-02.pdf --pages 6,19,21 --out july02-v34.json

# optional explicit page-image directory
python run_v3.py July-02.pdf --pages 6,19,21 --out july02-v34.json --page-dir ./july02-pages

# skip the recall sweep (roughly halves model calls)
python run_v3.py July-02.pdf --out full.json --no-audit
```

`--dump-crops` writes, per page: `aligned.png`, `handwriting.png` (the isolated
layer — look at this first when something is wrong), and `mark-NN.png` /
`mark-NN.json` for every mark with its candidate list and geometry hint. That
directory is the debugging surface: if a code is missed, the question is only
ever *"did the localizer make a mark?"* or *"did the reader read the crop
wrong?"*, and the crop tells you which.

Localizer output on July-02, no model calls:

```
page  1   4 marks in table band  (99214 · E11.65 · 93923+I70.213 · 83036)
page  6   1 mark                 (99395)
page 19   1 mark + 2 margin marks with no locked row in range
page 21   1 mark                 (99214)
```

## Cost

1–4 crop calls per page (~1.5 average on this batch) plus 2 panel calls for the
audit, against v2.4's full-page locked-universe review. Crops are small. Marks
with no candidate rows are rejected without a model call.

## Tuning, in order

1. `handwriting.clearance_min` — raise if halo survives, lower if faint pencil
   is lost. Check `handwriting_layer.components_dropped_as_halo` first.
2. `mark_localizer.bridge_kernel` — raise if one loop splits into two marks
   (visible as duplicate crops of the same region), lower if two nearby loops
   merge into one crop. Merging is the safer failure: the reader returns both.
3. `mark_localizer.write_in_regions` — **template geometry, recalibrate with the
   template.** Column A's last code ends at y=1345 and a legitimate column B
   circle reaches no further left than x≈396, which is why the insurance box is
   bounded at x<390.
4. `CHARGESHEET_MIN_PHYSICAL_COVERAGE` (default `0.50`) — adjacent-row material-overlap gate; tune only against the full regression set. `PROMOTE_CONFIDENCE` and `HINT_MARGIN` remain compatibility/diagnostic settings and do not decide ownership.

## Known limits

* `page_audit` coordinates are approximate; `min_separation` (55px) governs
  dedupe against geometric marks. Too small and you re-adjudicate the same loop;
  too large and a genuinely adjacent missed loop is swallowed.
* Marks in the description column with no locked row in range (margin arrows,
  the blue notes on page 19) are rejected without a model call. If those ever
  need to be captured as notes, widen `candidate_pad_x` rather than loosening
  the localizer.
* The write-in regions are specific to this template. A second form ships its
  own regions with its own catalog.
