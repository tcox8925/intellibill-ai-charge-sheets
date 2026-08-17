# Charge-sheet extraction: v0 (LLM) vs v1 (locked-template CV) vs v2 (locked-template CV, rewritten detector)

Analysis only — nothing has been integrated yet. `chargesheet_extraction_v2` has not been copied
into the repo.

## What v2 actually is

v2 is **not** a new architecture — it's the same locked-template, deterministic-geometry approach
as v1 (`v1_computer_vision/`), from the same vendor package, with one component rewritten:
`locked_template.py`'s circle-detection algorithm. Confirmed by diffing every file:

| File | v1 → v2 | Verdict |
|---|---|---|
| `extract.py` (AI header/notes) | byte-identical | untouched |
| `render.py` (pdftoppm) | byte-identical | untouched |
| `model_client.py` | byte-identical | untouched |
| `dates.py` | byte-identical | untouched |
| `verify_template.py` | byte-identical | untouched |
| `migrations/001_chargesheet.sql` | byte-identical | **no DB migration needed** |
| `templates/.../manifest.json`, `catalog.json` | byte-identical | **same approved template, no recalibration** |
| `requirements.txt` | byte-identical | no new dependencies |
| `settings.py` | 66→59 lines | removed our custom `CHARGESHEET_MATCH_MIN` override hook (see Integration notes) |
| `template_registry.py` | 72→70 lines | same removal, downstream of settings.py |
| `run.py` | 216→224 lines | wires up the new `possible_marks` return value + new flags |
| `locked_template.py` | 350→667 lines | **the actual rewrite — nearly 2x the code** |

Test suite: v2 ships 11 tests (v1 had far fewer), all passing (`11/11 OK`, verified by running it
myself) — 6 of which are new regression cases specifically for the rewritten detector (incomplete
circles, split fragments, checkmark rejection, adjacent-row ambiguity, two-real-adjacent-circles,
black-circle-with-strong-alignment).

## The core change: circle detection, v1 vs v2

**v1's approach:** for each catalog code, look for a complete-ish ellipse (`color_ellipse_min`,
`black_min`/`black_strong`/`black_pair_min` + a fixed `black_local_margin` for neighbor
disambiguation). Binary-ish: a mark either clears the ellipse-fit threshold or it doesn't.

**v2's approach:** for each catalog code, ask "is there enough ring/arc-shaped ink around this
coordinate to support a hand-drawn circle?" — evaluated via 12 angular sectors, longest continuous
arc, 4-side coverage, ring capture, and interior-ink penalty, combined into one weighted score
(`0.36*ring_coverage + 0.24*sector_coverage + 0.18*longest_arc + 0.12*side_coverage +
0.10*ring_capture - 0.05*interior_penalty`). This is a fundamentally more granular, sector-based
geometry model instead of a single ellipse-fit score.

Practical effect: v2 explicitly targets three real-world failure modes v1 handled poorly —
**incomplete/disconnected circles**, **circles crossing printed grid lines or text**, and
**checkmarks/slashes/underlines being mistaken for circles** (the interior-ink penalty is new and
specifically defends against this last one).

## New: `possible_marks` — ambiguity now has a real output, not silent guessing

This is the single biggest behavioral change. v1's `run.py` already had a `possible_marks` field in
its JSON schema, but it was hardcoded to `[]` — v1's `detect()` never actually returned anything for
it (3-tuple return: procedures, diagnoses, geometry). v2's `detect()` returns a 4-tuple, and
ambiguous marks — genuinely uncertain evidence that isn't safe to auto-confirm — now show up there
with a reason, candidate codes + scores, and pixel-coordinate `review_region` for a human reviewer,
instead of either being silently dropped or (worse) silently guessed:

- `adjacent_code_overlap` — one physical mark plausibly belongs to two neighboring codes; both
  candidates + scores are preserved, neither is auto-picked unless one is a clear winner
  (`candidate_winner_margin`, default 0.10) or both are independently strong
  (`adjacent_pair_strong_min`/`_sectors`/`_longest`/`_center_dx_max`/`_center_dy_max` — two
  genuinely-circled adjacent codes can now both be confirmed instead of one always winning).
- `partial_circle_low_confidence` — ring evidence exists but isn't strong enough to auto-confirm.

New page flags surface this: `circle_review_required`, `ambiguous_circle_assignment`,
`partial_circle_low_confidence`. Confirmed selections still only ever land in
`circled_procedures`/`circled_diagnoses` — `possible_marks` is explicitly designed to feed a human
review queue and is contractually barred from `wpo.chargesheet_selections` (per the schema, which is
identical to v1's).

## Tunability: v1 had 9 thresholds, v2 has ~25, all documented

v1's `DEFAULT_THRESHOLDS` was a flat 9-key dict with essentially no accompanying documentation
beyond code comments. v2 ships the same override mechanism (`thresholds` dict passed to
`LockedTemplate.__init__`, still non-invasive to call) but with ~25 named knobs, each with a
recommended tuning direction, risk tradeoff, and symptom-based lookup table in the 32-page guide —
e.g. "colored partial circle keeps landing in `possible_marks` instead of confirming" →
`color_confirm_min`, lower slightly, risk = more false positives. Two production-safety values are
explicitly called out as **do-not-touch-for-recovery**: `match_min` (0.78, unchanged from v1) and
`black_alignment_min` (0.90, unchanged from v1) — both protect against exactly the false-negative
scenario we already hit and fixed once with the `CHARGESHEET_MATCH_MIN` override.

## What this means for the false-negative issue we already fixed

`match_min` is still hardcoded to 0.78 in v2's `DEFAULT_THRESHOLDS` (unchanged from v1's default —
confirmed by diff). **v2 dropped the `CHARGESHEET_MATCH_MIN` settings.py override we added
ourselves** to v1 (that was our own patch, not part of the vendor package, so a fresh v2 drop
naturally doesn't have it). If we adopt v2, we need to reapply the same non-invasive override to
v2's `settings.py`/`template_registry.py` — same one-line change as before — or v2 will silently
revert to the over-strict 0.78 threshold that caused 7 of 11 false-negative chargesheet rejections
in production.

## Integration impact on our existing wrapper

Our `v1_computer_vision/pipeline_adapter.py` currently does `template.detect(alignment)` expecting a
3-tuple (`procedures, diagnoses, geometry`). v2's `detect()` returns a 4-tuple
(`procedures, diagnoses, possible_marks, geometry`). Adopting v2 means the adapter needs a small,
mechanical update to unpack 4 values and thread `possible_marks` through to `build_metrics`/logging —
consistent with the "wrapper only, don't touch core logic" approach used for v1.

## Recommendation

v2 is a strict improvement with near-zero migration cost: same DB schema, same approved template
(no recalibration), same AI/header boundary, same safety-critical thresholds (`match_min`,
`black_alignment_min` unchanged), same integration shape our adapter already expects (just a 4-tuple
instead of 3). The only things that need to travel with it are our own two prior patches:
1. Re-apply the `CHARGESHEET_MATCH_MIN` settings override.
2. Update `pipeline_adapter.py` to unpack `possible_marks` and (optionally) surface
   `circle_review_required`/`ambiguous_circle_assignment` in our per-page logging, since that's new,
   actionable review-queue data v1 never produced.

Suggested next step, when you're ready: copy `chargesheet_extraction_v2` into the repo as
`v2_computer_vision/` (same non-invasive, side-by-side pattern used for v1), apply the two patches
above, and run the reprocessing/threshold-validation exercise we already did for v1's false
negatives against v2 to confirm the new detector doesn't regress on those same real pages.
