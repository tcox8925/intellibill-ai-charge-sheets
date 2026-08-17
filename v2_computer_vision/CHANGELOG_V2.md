# Charge-sheet extraction v2 changes

## Circle detection
- Replaced connected-component-first ellipse matching with per-code partial-arc/ring scoring.
- Evaluates 12 angular sectors, longest continuous arc, side coverage, ring coverage, local ring capture, and interior ink.
- Handles incomplete circles, disconnected pen strokes, and circles crossing printed grid/text more reliably.
- Uses the same partial-arc logic for black circles after safe template subtraction when alignment >= 0.90.

## Ambiguity / exceptions
- `LockedTemplate.detect()` now returns `possible_marks` in addition to confirmed procedures, diagnoses, and geometry debug data.
- A clear winner is auto-selected only when adjacent evidence is not itself a confirmed competing circle.
- A circle plausibly covering two adjacent codes is not guessed; both candidate codes and scores are emitted in `possible_marks` with `review_region` coordinates.
- Two independently strong adjacent circles can still both be confirmed.
- Low-confidence partial circles are emitted as `possible_marks` rather than confirmed selections.

## JSON / flags
- `run.py` writes `possible_marks` into the same page JSON object.
- Pages with possible marks receive `circle_review_required`.
- Adjacent-code ambiguity also receives `ambiguous_circle_assignment`.
- Low-confidence partial circles also receive `partial_circle_low_confidence`.
- Confirmed selection `detection` values remain `color_geometry` or `residual_geometry` for DB compatibility; `geometry_mode` identifies `partial_arc` vs `full_loop`.

## Versioning / tests
- Pipeline version bumped to `locked-coordinate-extraction-v2`.
- Locked form/template remains v1; its reference image/catalog checksums were not changed.
- Added regression tests for incomplete colored circles, split circle fragments, incomplete black circles, ambiguous overlap between adjacent codes, two real adjacent circles, checkmark rejection, and existing fail-closed behavior.
