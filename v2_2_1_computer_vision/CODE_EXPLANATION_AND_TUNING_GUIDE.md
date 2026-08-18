# Charge-Sheet Extraction v2 — Code Explanation and Detection Tuning Guide

## 1. Purpose of this guide

This document explains how the charge-sheet extraction package works, where each decision is made, how circle exceptions are represented, and which settings to change when detection is too strict or too loose.

The most important architectural rule is unchanged:

> **Billing-code selection is deterministic. AI never decides CPT, HCPCS, or ICD selections.**

The locked form geometry determines confirmed selections. AI is limited to variable header/payment fields and handwritten clinical notes.

---

## 2. Core safety contract

The pipeline follows four rules:

1. **Known form only.** Every page is registered against the approved locked template.
2. **Fail closed.** If the template match is below the required minimum, no codes are returned.
3. **Circle only.** Checks, underlines, random handwriting, and AI guesses cannot become confirmed billing-code selections.
4. **Ambiguity stays in the same JSON.** A mark that looks real but cannot be assigned safely is placed in `page.possible_marks`; it is not inserted into `circled_procedures` or `circled_diagnoses`.

Confirmed selections can therefore flow to `wpo.chargesheet_selections`, while review items remain in the page `raw_result` JSON.

---

## 3. Package map

| File / folder | Responsibility |
|---|---|
| `run.py` | Main orchestration. Renders pages, aligns them, runs deterministic code detection, optionally runs header/note AI, and assembles the final JSON. |
| `locked_template.py` | Core deterministic logic: orientation, ORB/ECC registration, colored-ink detection, black-ink template subtraction, partial-arc scoring, adjacent-code resolution, and `possible_marks`. |
| `render.py` | Renders PDFs with Poppler `pdftoppm` at the locked DPI. |
| `template_registry.py` | Loads the approved template, verifies checksums, renderer contract, and safety policy. |
| `extract.py` | AI transcription of header/payment fields and handwritten clinical notes only. Billing codes are explicitly forbidden from the model output. |
| `model_client.py` | Creates the model client from direct credentials or Azure Key Vault/Foundry. |
| `dates.py` | Normalizes service date and DOB fields and adds date-quality flags. |
| `settings.py` | Runtime/template/model settings. |
| `verify_template.py` | Geometry-only verification utility. No AI or database calls. |
| `templates/.../catalog.json` | Fixed code locations, rows, columns, descriptions, and template policy. |
| `templates/.../reference.png` | Approved blank/reference form used for alignment and black-ink subtraction. |
| `templates/.../manifest.json` | Immutable template metadata, checksums, renderer, DPI, and version. |
| `migrations/001_chargesheet.sql` | Minimal PostgreSQL schema for templates, documents, pages, confirmed selections, and notes. |
| `tests/` | Regression/contract tests, including partial circles, split circles, black circles, ambiguity, and fail-closed behavior. |

---

## 4. End-to-end extraction flow

```text
PDF bytes
   |
   v
render.py -> pdftoppm at 200 DPI
   |
   v
page PNG
   |
   v
LockedTemplate.align()
   |-- orientation estimate
   |-- ORB feature registration
   `-- ECC affine refinement
   |
   +-- template_match < match_min (0.78)
   |      `-> FAIL CLOSED: zero code selections
   |
   v
LockedTemplate.detect()
   |-- score colored ink around every known code
   |-- if match >= black_alignment_min (0.90):
   |      subtract aligned page from clean reference
   |      and score new black ink around every known code
   |-- merge color + black candidates
   |-- resolve adjacent candidates
   |      |-- clear winner -> confirmed
   |      |-- two independent real circles -> both confirmed
   |      `-- ambiguous -> possible_marks
   |
   v
confirmed procedure/diagnosis arrays + possible_marks + geometry debug
   |
   +-- optional extract.py AI transcription
   |      header/payment + clinical notes only
   |
   v
run.py assembles one JSON result
```

---

## 5. `run.py` — orchestration and JSON contract

`process_pdf()` is the main entry point.

For every rendered page it:

1. Decodes the PNG.
2. Calls `template.align()`.
3. Fails closed immediately if the locked template does not match.
4. Calls `template.detect()` to obtain:
   - confirmed procedures,
   - confirmed diagnoses,
   - `possible_marks`,
   - geometry/debug information.
5. Optionally extracts header and clinical notes with AI.
6. Normalizes date fields.
7. Builds page flags.
8. Returns everything in the same page JSON.

### 5.1 Where exceptions live

There is no separate exception JSON produced by this package. Reviewable circle issues live here:

```json
{
  "page": 1,
  "circled_procedures": [],
  "circled_diagnoses": [],
  "possible_marks": [
    {
      "type": "ambiguous_circle",
      "reason": "adjacent_code_overlap",
      "kind": "procedure",
      "candidate_codes": [
        {"code": "99213", "score": 0.67, "source": "color_geometry"},
        {"code": "99214", "score": 0.64, "source": "color_geometry"}
      ],
      "winner_margin": 0.03,
      "review_region": {"x1": 410, "y1": 610, "x2": 590, "y2": 700}
    }
  ],
  "flags": [
    "locked_coordinate_selection",
    "circle_review_required",
    "ambiguous_circle_assignment"
  ]
}
```

The code intentionally does **not** copy either candidate into a confirmed code array.

### 5.2 Main possible-mark reasons

- `adjacent_code_overlap`: one physical mark plausibly maps to two or more neighboring codes.
- `partial_circle_low_confidence`: circle-like evidence exists around one code but is not strong enough for auto-selection.
- `partial_circle_adjacent_to_confirmed`: a confirmed neighboring circle exists, but another nearby mark is strong enough to preserve for review rather than discard.

### 5.3 Page flags

Important flags include:

- `locked_template_mismatch`
- `skipped_no_extraction`
- `locked_coordinate_selection`
- `color_circle_geometry`
- `black_circle_geometry`
- `circle_review_required`
- `ambiguous_circle_assignment`
- `partial_circle_low_confidence`
- `black_circle_detection_suppressed_low_alignment`
- `header_notes_reused_from_cache`

---

## 6. `locked_template.py` — the deterministic detector

This is the file to understand before changing detection behavior.

### 6.1 `DEFAULT_THRESHOLDS`

All normal tuning knobs are centralized at the top of `locked_template.py` in `DEFAULT_THRESHOLDS`.

This includes not only confidence thresholds but also the search window, expected hand-circle sizes, color cleanup, pre-gates, score weights, and adjacent-row resolver thresholds.

`LockedTemplate.__init__()` copies the defaults and optionally applies overrides:

```python
self.thresholds = dict(DEFAULT_THRESHOLDS)
self.thresholds.update(thresholds or {})
```

That makes tests/experiments possible without modifying the global defaults:

```python
t = LockedTemplate(
    catalog=catalog,
    reference_path=str(reference),
    thresholds={"color_confirm_min": 0.45},
)
```

The production `locked_template()` factory currently uses the defaults.

---

## 7. Alignment and template recognition

### 7.1 Orientation

`align()` first compares edge overlap in the likely orientations and chooses the best rotation.

### 7.2 ORB registration

`_orb_align()` finds ORB keypoints on the page and the reference and estimates a partial affine transform with RANSAC.

The OpenCV RNG is pinned:

```python
cv2.setRNGSeed(8342026)
```

so repeated runs do not vary because RANSAC selected a different random sample.

### 7.3 ECC refinement

`_ecc_refine()` uses `cv2.findTransformECC()` to refine the affine registration against the clean reference.

Its correlation coefficient becomes the primary `template_match` score.

### 7.4 Fail-closed boundary

```python
match_min = 0.78
```

If `template_match < match_min`, `detect()` returns zero selections and a `locked_template_mismatch` error.

**Do not lower this just to recover more circles.** If a page is not registered well enough, every code coordinate becomes less trustworthy.

---

## 8. How the partial-circle detector works

### 8.1 Important design change from v1

The detector does **not** start by finding a complete connected circle object.

Instead, for each known code coordinate it asks:

> Is there enough ring/arc-shaped ink around this code to support a hand-drawn circle?

That is why disconnected pen strokes, incomplete loops, and circles cut by printed grid/text can still be detected.

### 8.2 `_ring_evidence()`

For one known code center, `_ring_evidence()`:

1. Crops a local region around the code.
2. Searches a small set of nearby center offsets.
3. Tries several expected horizontal and vertical radii.
4. Draws a candidate elliptical ring.
5. Measures how much observed ink lies on that ring.
6. Splits the ellipse into 12 angular sectors.
7. Measures how many sectors have sufficient ink.
8. Finds the longest continuous circular run of hit sectors.
9. Measures coverage across four sides/quadrants.
10. Measures unwanted interior ink.
11. Measures how much local ink is explained by the candidate ring.
12. Returns the best-scoring geometry.

### 8.3 Evidence fields

A candidate can contain:

```json
{
  "score": 0.74,
  "ring_coverage": 0.58,
  "sector_coverage": 8,
  "longest_arc_sectors": 6,
  "side_coverage": 0.75,
  "interior_ink": 0.09,
  "ring_capture": 0.63,
  "center_offset": [5, 0],
  "radius": [54, 19]
}
```

Meaning:

- `score`: combined detector score used for candidate decisions.
- `ring_coverage`: percentage of expected ring pixels containing detected ink.
- `sector_coverage`: how many of 12 ring sectors contain enough ink.
- `longest_arc_sectors`: longest continuous run of present sectors. Five sectors is roughly 150 degrees of continuous arc.
- `side_coverage`: fraction of four broad sides that contain meaningful ring ink.
- `interior_ink`: ink inside the ring; used as a small negative signal against checks/slashes/scribbles.
- `ring_capture`: how much nearby foreground ink is explained by the ring itself.
- `center_offset`: best hand-circle center relative to the printed code center.
- `radius`: best fitted ellipse radii in locked-template pixels.

---

## 9. Colored-circle path

`_score_color_cells()` converts the aligned page to HSV and builds a colored-ink mask.

Current defaults:

```python
color_sat_min = 60
color_value_min = 50
```

A small morphological close joins tiny gaps in colored pen strokes.

The detector then loops over **every catalog code**, performs a cheap ink pre-gate, and only then runs `_ring_evidence()`.

This is intentionally code-centric rather than connected-component-centric.

---

## 10. Black-circle path

Black circles cannot be isolated by saturation. Instead, when alignment is strong enough, the code computes:

```python
residual = cv2.subtract(self.ref, alignment.gray)
```

The reference already contains the printed form. Ideally the residual therefore contains new/darker handwriting and marks.

### 10.1 Black alignment safety threshold

```python
black_alignment_min = 0.90
```

Black residual detection is disabled below this value because even small registration errors can turn printed text/grid lines into false residual ink.

A page can therefore be:

- a valid locked template (`match >= 0.78`), while
- black-circle detection is intentionally suppressed (`match < 0.90`).

Colored circles can still be detected in that range.

### 10.2 Color takes precedence

When the same cell has both colored and black-residual evidence, the colored geometry is authoritative.

Black residual candidates adjacent to a colored candidate are also suppressed to reduce duplicate/spillover detections caused by the same physical colored mark.

---

## 11. Confirmed candidate rules

`_is_confirmed_candidate()` requires **all** of the following:

- score above the source-specific confirm threshold,
- enough sectors,
- a sufficiently long continuous arc,
- enough broad-side coverage.

Current defaults:

| Rule | Colored | Black |
|---|---:|---:|
| possible score minimum | 0.30 | 0.32 |
| confirmed score minimum | 0.42 | 0.44 |
| confirmed sectors | 6 of 12 | 6 of 12 |
| longest continuous arc | 5 sectors | 5 sectors |
| side coverage | 0.50 | 0.50 |

The difference between `possible_*` and `confirm_*` is intentional:

- below `possible_min`: ignore as weak/noise,
- at/above `possible_min` but not confirmed: preserve as `possible_marks`,
- confirmed: eligible for automatic selection/resolution.

---

## 12. Adjacent-code resolution

`_resolve_candidates()` prevents one large/misaligned circle from selecting neighboring codes.

Candidates are grouped by code column and adjacent row number.

### 12.1 Single candidate

- Confirmed -> selected.
- Possible but not confirmed -> `partial_circle_low_confidence`.

### 12.2 Multiple adjacent candidates

The resolver first asks whether at least two candidates are **independently strong and independently centered**.

Current strong-pair requirements include:

```python
adjacent_pair_strong_min = 0.68
adjacent_pair_sectors = 9
adjacent_pair_longest = 8
adjacent_pair_center_dx_max = 6
adjacent_pair_center_dy_max = 5
```

If two real adjacent circles satisfy that test, both are selected.

Otherwise the candidates are ranked. The original clear-winner path still auto-selects when the best candidate is confirmed, the second candidate is not confirmed, and the score margin is at least:

```python
candidate_winner_margin = 0.10
```

v2.1 adds a narrow deterministic resolver path for the case where **both adjacent candidates are independently confirmed**. No global thresholds are lowered. The stronger candidate may win only when the fitted geometry shows that it owns the physical mark: its fitted loop is centered on its own locked row, while each confirmed adjacent competitor is vertically shifted toward that winning row. This is the expected signature of one physical circle spilling into a neighboring row's detection window.

If both candidates fit their own row centers, if two candidates satisfy the independently-strong pair rules, or if geometric ownership is otherwise unclear, the resolver does **not** force a winner. The group remains `adjacent_code_overlap` in `possible_marks`.

---

## 13. Detection confidence shown in confirmed output

Confirmed output uses:

```python
confidence = min(0.99, 0.58 + score * 0.46)
```

This is an **output/reporting confidence**, not the decision threshold.

Changing that formula will change the number displayed to downstream consumers but will **not** change whether a code is selected. Selection is controlled by the detector thresholds described above.

---

# PART II — WHAT TO TWEAK

## 14. First rule of tuning

Do not change several detector families at once.

For every tuning change:

1. Keep a labeled set of real pages containing good circles, incomplete circles, black circles, two adjacent real circles, ambiguous marks, checks, underlines, and blanks.
2. Run geometry only (`--no-ai`).
3. Compare confirmed selections and `possible_marks` to the expected answer.
4. Change one threshold family.
5. Re-run the complete regression set.
6. Add any real production failure pattern to `tests/` before accepting the new default.

The objective is not “lowest exception rate.” It is:

> **Maximum automatic recall without allowing ambiguous or non-circle marks to become confirmed billing codes.**

---

## 15. Safe first-line tuning knobs

These are the parameters to adjust first because their behavior is easy to reason about.

### 15.1 `color_confirm_min` — default `0.42`

Controls how high the combined score must be for a colored candidate to be confirmable.

- Lower it -> more colored partial circles auto-confirm; higher false-positive risk.
- Raise it -> fewer false positives; more real circles go to `possible_marks`.

Recommended use: if real colored partial circles already appear in `possible_marks` with good geometry but fail confirmation.

### 15.2 `black_confirm_min` — default `0.44`

Same role for black/template-residual marks.

Be more conservative with black than color because residual noise can come from imperfect alignment.

### 15.3 `ring_confirm_sectors` — default `6`

Number of 12 sectors that must be present.

- Lower to 5 -> allows more incomplete loops.
- Raise to 7+ -> requires more of the loop to exist.

### 15.4 `ring_confirm_longest` — default `5`

Minimum continuous run of present sectors.

This is one of the best checkmark/scribble protections.

- Lower -> accepts more fragmented partial circles.
- Raise -> demands a longer coherent arc.

### 15.5 `ring_confirm_sides` — default `0.50`

Requires ring evidence on at least half of the four broad sides.

- Lower -> more tolerant of circles missing an entire side.
- Raise -> stronger defense against one-sided hooks/underlines.

### 15.6 `candidate_winner_margin` — default `0.10`

Controls adjacent-code ambiguity.

- Lower (for example 0.10 -> 0.07) -> easier to auto-select the top neighboring code; **fewer exceptions, more wrong-neighbor risk**.
- Raise (for example 0.10 -> 0.15) -> requires a clearer winner; **more exceptions, safer assignments**.

This is the primary knob when overlap between neighboring codes is the issue.

---

## 16. Sensitivity / “did we notice the mark at all?” knobs

These affect whether a weak mark becomes a candidate before confirmation.

### 16.1 `color_possible_min` — default `0.30`

- Lower -> more weak colored marks are preserved for review.
- Raise -> less noise in `possible_marks`, but weak true circles can disappear entirely.

If a real mark does **not appear anywhere** in confirmed selections or `possible_marks`, this is more relevant than `color_confirm_min`.

### 16.2 `black_possible_min` — default `0.32`

Same for black residual geometry.

### 16.3 `color_pre_gate_min` — default `0.004`

Cheap minimum colored-ink density before running the expensive ring search around a code.

- Lower -> investigate more nearly-empty cells; more sensitive, slightly slower/noisier.
- Raise -> skip more cells; faster but easier to miss very faint/small marks.

### 16.4 `black_pre_gate_min` — default `0.012`

Equivalent pre-gate for residual black ink.

### Important distinction

`pre_gate` determines whether we run ring analysis at all.

`possible_min` determines whether analyzed ring evidence is kept as a candidate.

`confirm_min` determines whether that candidate is strong enough to auto-select, subject to arc/side rules and adjacent resolution.

---

## 17. Faded or unusual colored pen

### `color_sat_min` — default `60`

HSV saturation threshold.

- Lower -> catches paler/faded colored pen, but may include more gray/scan artifacts.
- Raise -> requires stronger color.

### `color_value_min` — default `50`

Minimum brightness/value for colored pixels.

Normally leave this alone unless scans are unusually dark.

### `color_morph_kernel` — default `5`

Morphological closing kernel size used to join tiny gaps in colored strokes.

- Increase modestly (odd values such as 7) -> joins wider gaps, but can merge nearby unrelated marks.
- Decrease to 3 -> less merging and stricter shape preservation.

Use odd positive values.

---

## 18. Black circles are being missed

First inspect:

```json
"residual_geometry_enabled": true
```

### If it is `false`

The page alignment is below `black_alignment_min`.

Do **not** immediately lower black score thresholds; they are not being used at all.

The preferred fix is better registration/scan quality. Lowering `black_alignment_min` reduces the safety buffer against printed-form residuals and should only be done after a large regression test.

### If residual geometry is enabled

Consider, in this order:

1. `residual_pixel_min` — default `40`
   - Lower -> fainter black residual ink is included.
   - Raise -> only stronger page/reference differences count.
2. `black_pre_gate_min` — default `0.012`.
3. `black_possible_min` — default `0.32`.
4. `black_confirm_min` — default `0.44`.
5. Arc/sector requirements if the mark is obviously incomplete.

Black thresholds should generally remain slightly stricter than colored thresholds.

---

## 19. Real incomplete circles are going to exceptions too often

Look at their evidence first.

### Case A: good score, too few sectors

Lower `ring_confirm_sectors` one step.

### Case B: many sectors but broken continuity

Lower `ring_confirm_longest` one step.

### Case C: circle is mostly on one/two sides

Lower `ring_confirm_sides` carefully.

### Case D: all geometry is decent but total score is just under confirm

Lower `color_confirm_min` or `black_confirm_min` slightly.

### Case E: mark does not appear in `possible_marks`

Tune `possible_min`, pre-gates, color/residual pixel threshold, or circle search geometry instead. Lowering only `confirm_min` cannot help a candidate that was never created.

---

## 20. Checks, slashes, or handwriting are becoming circles

Tighten in this order:

1. Raise `ring_confirm_longest`.
2. Raise `ring_confirm_sectors`.
3. Raise `ring_confirm_sides`.
4. Raise the relevant `*_confirm_min`.
5. As an advanced change, increase `score_interior_penalty_weight` modestly.

The interior penalty exists because a check/slash often puts disproportionate ink inside the expected circle instead of around the ring.

Do not make the interior penalty too strong: a legitimate circle can cross printed code text and therefore contain interior residual ink.

---

## 21. One circle is selecting the wrong neighboring code

Use a **safer resolver**, not a looser detector.

Recommended changes:

- Raise `candidate_winner_margin` (e.g. 0.10 -> 0.12 or 0.15).
- Tighten `adjacent_pair_center_dx_max` / `adjacent_pair_center_dy_max` if off-center spillover is being treated as an independent circle.
- Raise `adjacent_pair_strong_min` if one large mark is being mistaken for two real adjacent circles.

These changes increase review/exceptions rather than silently guessing.

---

## 22. Two genuinely circled adjacent codes are going to one exception

This is the opposite problem.

The two circles must independently satisfy the strong-pair rules.

Tune carefully:

- Lower `adjacent_pair_strong_min` slightly.
- Lower `adjacent_pair_sectors` by one.
- Lower `adjacent_pair_longest` by one.
- Relax `adjacent_pair_center_dx_max` / `adjacent_pair_center_dy_max` if real circles are consistently centered away from the printed code.

Do **not** start by lowering `candidate_winner_margin`; that controls single-winner overlap, not recognition of two independently real circles.

---

## 23. Expected circles are larger, smaller, or shifted

The detector searches a fixed family of likely hand-circle shapes. These are now centralized in `DEFAULT_THRESHOLDS`.

### Current center offsets

```python
ring_center_dx_values = (-10, -5, 0, 5, 10)
ring_center_dy_values = (-5, 0, 5)
```

If users systematically draw circles farther left/right/up/down, extend these sets.

Tradeoff: a wider center search is more tolerant but increases the chance that a neighboring mark fits the wrong code.

### Current ellipse radii

```python
ring_rx_values = (30, 42, 54, 66)
ring_ry_values = (13, 19, 25)
```

If users draw substantially larger circles, add a larger radius rather than replacing all existing values.

Example:

```python
ring_rx_values = (30, 42, 54, 66, 78)
ring_ry_values = (13, 19, 25, 31)
```

Then verify neighboring-row ambiguity carefully.

### Search patch

```python
ring_patch_half_width = 90
ring_patch_half_height = 45
```

If you add larger radii or larger center offsets, the patch may also need to grow.

---

## 24. Ring thickness and local cleanup

### `ring_stroke_width` — default `5`

This is the thickness of the *expected synthetic ring* used to test whether observed ink lies near the ellipse.

- Increase -> more spatial tolerance around the expected ellipse; can capture nearby noise.
- Decrease -> more precise fit; can miss rough/thick handwriting.

This is not the actual pen thickness.

### `ring_outer_padding` — default `8`

Controls the local area used for `ring_capture`.

### `ring_inner_rx_scale` / `ring_inner_ry_scale`

Define the central interior region used for the unwanted-interior-ink signal.

These are advanced knobs. Change only with visual/evidence inspection.

---

## 25. Sector hit thresholds

### `ring_sector_hit_min` — default `0.18`

A sector counts as “present” when at least this fraction of its expected ring pixels contain ink.

- Lower -> sparse arcs make sectors count more easily.
- Raise -> each sector must contain denser ring ink.

### `ring_side_hit_min` — default `0.16`

Similar threshold for each broad side/quadrant.

These thresholds affect `sector_coverage`, `longest_arc_sectors`, and `side_coverage`, so they can have a larger downstream effect than changing one confirm threshold.

---

## 26. Score weights — advanced tuning

Current combined score:

```text
0.36 * ring coverage
0.24 * sector coverage
0.18 * longest continuous arc
0.12 * side coverage
0.10 * ring capture
- 0.05 * interior ink penalty
```

Configured as:

```python
score_ring_coverage_weight = 0.36
score_sector_coverage_weight = 0.24
score_longest_arc_weight = 0.18
score_side_coverage_weight = 0.12
score_ring_capture_weight = 0.10
score_interior_penalty_weight = 0.05
score_interior_penalty_cap = 0.60
```

Treat weight changes as advanced calibration. Usually threshold/geometry changes are easier to validate and explain.

If changing positive weights, keep their total approximately 1.0 so existing score thresholds remain interpretable. Otherwise you must recalibrate `possible_min`, `confirm_min`, and adjacent thresholds as well.

---

## 27. Parameters that should usually NOT be tuned as a detection shortcut

### `match_min`

This protects locked-coordinate validity. Lowering it can make every downstream code location unreliable.

### `black_alignment_min`

This protects residual subtraction from registration artifacts. Only lower after explicit black-residual validation.

### Render DPI / renderer

The template is calibrated to `pdftoppm` at 200 DPI. Changing DPI or renderer is a **template calibration/version change**, not a normal detector-threshold tweak.

### `catalog.json` coordinates

If the printed form itself moved or changed, create/recalibrate a new template version. Do not “fix” a new form layout by aggressively widening detector thresholds.

---

## 28. Tuning lookup table

| Symptom | First knobs | Direction | Main risk |
|---|---|---|---|
| Incomplete colored circle appears in `possible_marks` | `color_confirm_min`, `ring_confirm_*` | Lower slightly | More false positives |
| Incomplete black circle appears in `possible_marks` | `black_confirm_min`, `ring_confirm_*` | Lower slightly | Residual false positives |
| Real colored mark not seen at all | `color_possible_min`, `color_pre_gate_min`, `color_sat_min` | Lower | More noise/review items |
| Real black mark not seen, residual enabled | `residual_pixel_min`, `black_pre_gate_min`, `black_possible_min` | Lower | Printed residual noise |
| Black residual disabled | Alignment / `black_alignment_min` | Prefer fixing alignment | False printed-form differences |
| Checks/slashes selected | `ring_confirm_longest`, sectors/sides, confirm score | Raise | More real partials to review |
| Too many adjacent-code exceptions | `candidate_winner_margin` | Lower carefully | Wrong-neighbor auto-selection |
| Wrong neighbor auto-selected | `candidate_winner_margin` | Raise | More exceptions |
| Two real adjacent circles become exception | `adjacent_pair_*` | Loosen carefully | Double-selection from spillover |
| Large circles missed | `ring_rx_values`, `ring_ry_values`, patch size | Add larger values | Neighbor overlap |
| Off-center circles missed | center offset values | Expand | Wrong-code fit |
| Faded colored ink missed | `color_sat_min` | Lower | Gray/scan noise |
| Broken colored strokes missed | `color_morph_kernel` | Increase modestly | Merge unrelated strokes |

---

## 29. Recommended tuning workflow with real pages

A practical calibration cycle:

### Step 1 — Build a labeled regression set

Keep examples of:

- clear full colored circles,
- incomplete colored circles,
- split/disconnected circles,
- circles crossing grid/text,
- black circles,
- two adjacent real circles,
- one ambiguous circle between two rows,
- checkmarks,
- underlines/slashes,
- blank rows,
- weak/marginal alignment pages.

### Step 2 — Run geometry only

```bat
python run.py sample.pdf --no-ai --out geometry_only.json
```

This removes model variability and cost from detector calibration.

### Step 3 — Inspect three outputs

For each problem page inspect:

1. confirmed arrays,
2. `possible_marks`,
3. `circle_detection.evidence` and residual enablement.

### Step 4 — Decide what stage failed

Ask:

- Was there enough ink to pass the pre-gate?
- Did it reach `possible_min`?
- Did it fail a confirm rule?
- Was it confirmed but blocked by adjacent ambiguity?
- Was black detection suppressed because alignment was too low?

Change the threshold for the **stage that actually failed**.

### Step 5 — Re-run all tests

```bat
python -m unittest discover -s tests -v
```

### Step 6 — Add the real failure pattern as a regression test

Do not rely only on the current synthetic tests. Production examples should gradually become a representative test corpus (with PHI removed/sanitized as required).

---

## 30. Current regression coverage

The package currently tests:

- incomplete circle auto-selection,
- split/disconnected colored arcs,
- incomplete black circle with strong alignment,
- centered-between-rows ambiguity,
- two legitimate adjacent circles,
- checkmark rejection,
- template artifact hashes,
- catalog uniqueness/types,
- fail-closed template policy,
- known catalog coordinates,
- black residual suppression at low alignment,
- confirmed-neighbor spillover resolution for `99395`,
- confirmed-neighbor spillover resolution for `36415`,
- fail-closed behavior when two confirmed adjacent candidates are both centered on their own rows.

The suite currently passes 14/14 tests.

---

## 31. AI boundary (`extract.py`)

`extract.py` is intentionally separate from billing-code geometry.

Its system prompt explicitly says to ignore code circles/checks/underlines and never identify or infer selected CPT/HCPCS/ICD codes.

The expected model JSON includes only:

```json
{
  "header": {
    "date": "",
    "name": "",
    "dob": "",
    "prn": "",
    "insurance": "",
    "copay": "",
    "amount_paid": "",
    "payment_type": ""
  },
  "notes": [],
  "flags": []
}
```

`_clean()` normalizes this output and deduplicates notes. `extract_header_notes()` retries transient failures and strips forbidden billing-code keys defensively even if a model returned them unexpectedly.

Do not add billing-code arrays to this model schema.

---

## 32. Header/note cache

`run.py` caches header/note extraction by:

```text
rendered page SHA-256 + model + header extractor version
```

This makes repeated extraction of the same page stable and avoids repeated model calls.

Use `--refresh-header-cache` only when intentionally re-running header/notes extraction.

The cache does not affect deterministic billing-code geometry.

---

## 33. Template registry and immutable assets

`template_registry.py` validates:

- configured template ID/version,
- renderer engine (`pdftoppm`),
- renderer DPI,
- `catalog.json` checksum,
- `reference.png` checksum,
- policy `runtime_code_source = locked_coordinates_only`,
- `ai_can_add_codes = false`,
- `selected_mark = circle_only`,
- `unknown_template = fail_closed`.

If the reference or catalog is deliberately changed, its manifest/checksum must be deliberately updated as part of a new approved template artifact/version process.

This protects against someone casually replacing form coordinates without realizing that code assignment logic has changed.

---

## 34. Database behavior

`migrations/001_chargesheet.sql` deliberately separates confirmed selections from raw page evidence.

### `wpo.chargesheet_pages`

Stores page-level flags, `circle_detection`, and the full `raw_result` JSON. This is where `possible_marks` are preserved when the ingestion layer stores the extraction result.

### `wpo.chargesheet_selections`

Stores **confirmed circles only**.

Its contract allows only:

- `kind`: procedure or diagnosis,
- `mark`: circle,
- `detection_source`: `color_geometry` or `residual_geometry`.

Ambiguous marks must never be inserted into this table before human resolution.

There is no dedicated review/exception table in this extraction-only package. A downstream workflow can read `raw_result -> possible_marks` and create its own work queue if needed.

---

## 35. What constitutes a new template version vs detector tuning

### Detector tuning only

Examples:

- slightly more tolerant incomplete-circle threshold,
- different winner margin,
- broader expected hand-circle radius,
- faded pen threshold,
- stronger checkmark rejection.

These change how marks are interpreted around the **same locked coordinates**.

### New/recalibrated template version

Examples:

- form layout changed,
- code rows moved,
- new/removed codes,
- columns moved,
- renderer or DPI changed,
- a new blank reference image is required.

Do not use threshold expansion to compensate for a genuinely different printed form.

---

## 36. Suggested conservative defaults for production changes

When adjusting from the current defaults, start with small moves:

- Score thresholds: approximately `0.02–0.04` at a time.
- `candidate_winner_margin`: approximately `0.02–0.03` at a time.
- Sector/longest requirements: one sector at a time.
- Center search: add one offset step rather than doubling the range.
- Radii: add one expected radius rather than replacing the existing family.
- Color saturation / residual intensity: change in modest increments and retest noise pages.

These are calibration practices, not universal target values. Real labeled pages should decide the final numbers.

---

## 37. Quick diagnostic decision tree

```text
A real circle was missed
|
+-- Is it in possible_marks?
|      |
|      +-- YES -> confirmation/resolver issue
|      |           inspect score, sectors, longest arc, sides, winner margin
|      |
|      `-- NO -> candidate-generation issue
|                  inspect pre-gate, possible_min, color/residual threshold,
|                  expected radius/offset search
|
+-- Is it black ink?
       |
       +-- residual_geometry_enabled = false
       |      -> alignment safety prevented black detection
       |
       `-- residual enabled
              -> inspect residual_pixel_min / black gates / ring geometry
```

```text
A wrong code was selected
|
+-- Neighboring code?
|      -> raise winner margin / tighten independent adjacent-pair rules
|
+-- Check/slash/scribble?
|      -> strengthen longest arc / sectors / sides / confirm score
|
`-- Page alignment weak?
       -> protect/fix registration; do not solve by making mark detection looser
```

---

## 38. Final principle

The detector should be **tolerant about how humans draw a circle** but **conservative about which billing code that circle belongs to**.

That is why v2 separates:

- mark sensitivity,
- ring-shape confirmation,
- code assignment,
- adjacent-code ambiguity,
- and final exception handling.

When tuning, preserve that separation. If the mark is clearly a circle but the code assignment is uncertain, the correct output is `possible_marks`, not a guessed billing code.
