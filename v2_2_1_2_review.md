# Review: `chargesheet_extraction_v2_2_1_2` (delivered 2026-08-20)

## Verdict

**This is a real, new code change — not a re-send of what we already have.**
It is exactly the fix needed to reproduce the vendor's "distinct-multicircle"
sample output you saw. Confirmed by diffing every file against our currently
integrated `v2_2_1_computer_vision/` and by running the vendor's own test
suite (31/31 pass, including 4 brand-new tests for this exact feature).

---

## 1. What actually changed (file-by-file diff)

Compared `chargesheet_extraction_v2_2_1_2/chargesheet_extraction_v2/` against
our currently-active `v2_2_1_computer_vision/`:

| File | Status | Notes |
|---|---|---|
| `mark_resolver.py` | **Changed** — this is the real fix | New partial-promotion path for `circle_multiple` adjacent-overlap groups |
| `settings.py` | **Changed** | New tunables + version strings; **our `CHARGESHEET_MATCH_MIN` override support was dropped again** (see §4) |
| `template_registry.py` | **Changed** | Just the mechanical consequence of the settings.py change above (no `match_min_override` to read anymore) |
| `run.py` | Identical | byte-for-byte same as what we have |
| `locked_template.py` | Identical | geometry/circle-detection engine untouched |
| `extract.py` | Identical | header/notes extraction untouched |
| `render.py` | Identical | PDF rendering untouched |
| `dates.py` | Identical | date normalization untouched |
| `model_client.py` | Identical | Anthropic/Foundry client wiring untouched |
| `auth.py`, `requirements.txt`, `migrations/`, `verify_template.py` | Identical | |
| `.env.example` | **Changed** | documents 2 new env vars (see §3) |
| `README_OUTPUT_AND_DISPLAY.md` | **New file** | consumer-facing output-contract guide (see §5) |
| `CODE_EXPLANATION_AND_TUNING_GUIDE.md`/`.docx` | Identical | **not updated** for this change — the new multi-circle promotion logic isn't documented there |
| `tests/test_mark_resolver.py` | **Changed** | +4 new test cases for the new logic |
| `tests/test_mark_resolver.py.tmp` | Stray artifact | 0-byte leftover file, harmless but should be excluded if we copy this in (same cleanup we've done on every prior drop) |

**Bottom line: this is a surgical, single-purpose patch.** The detection/geometry
engine is completely untouched — only the AI mark-resolution policy changed.
This matches what we already inferred from comparing the two JSON outputs
(near-identical geometry scores, only the promotion decision differed).

---

## 2. The actual fix, explained

### Before (what we're running: `..._manual_proc_review`)

In `apply_mark_review()`, for an `adjacent_code_overlap` mark, promotion was
only possible via one narrow path:

```python
can_promote = (
    office_only_selection
    and conf >= s.mark_promote_confidence   # 0.85
    and cls == "circle_single"              # <-- circle_multiple never qualifies
    and len(mapped_selected) == 1
    and selected_geometry_eligible == [True]
)
```

Any adjacent-overlap group the AI classified as `circle_multiple` (two or
more genuinely distinct hand-drawn loops) was **always** forced into manual
review, no matter how strong the underlying geometry was for any individual
code in the group.

### After (`..._distinct_multicircle`)

A second, independent promotion path is added, gated by two new settings:

- `CHARGESHEET_MARK_MULTI_PROMOTE_CONFIDENCE` (default `0.55`) — minimum AI
  confidence in its `circle_multiple` classification itself.
- `CHARGESHEET_MARK_MULTI_STRONG_SCORE` (default `0.58`) — minimum
  deterministic geometry score an *individual* candidate must have to be
  promoted (separate from, and higher than, the multi-classification
  confidence gate above).

For each row the AI selected inside a `circle_multiple` adjacent-overlap
group, a row is promoted **individually** only if **all** of:
1. its locked catalog `kind` is `procedure` (diagnoses are *never* eligible
   via this path — enforced unconditionally, not section-gated);
2. its own deterministic score ≥ `mark_multi_strong_score` (0.58);
3. it also separately clears the existing source-specific confirm threshold
   (`_candidate_has_confirmation_level_evidence` — same black/color
   confirm-score gate every other promotion path already uses).

Rows in the same group that don't clear this bar (weak spillover, e.g. a
neighboring lab code the circle merely grazes) are **not** promoted — they
stay attached to the mark, which itself remains in `possible_marks` with
`manual_review: true` unless *every* candidate in the group got promoted.

The mark's own audit trail is enriched accordingly:
- `ai_review.promoted_candidate_codes` — which codes from this group were promoted.
- `ai_review.partial_promotion: true` — set when some (not all) candidates in the group were promoted, i.e. the mark still needs review for the leftover ones.
- promoted items get `"resolution": "constrained_ai_distinct_multiple_circles"` instead of `"constrained_ai_visual_review"`, so confirmed output is still traceable to exactly which policy path confirmed it.

This is intentionally still very conservative: no code can be invented (the
AI still only picks from pre-existing geometry candidates, same as before),
diagnoses can never use this path at all, and a weak candidate can't ride
along just because a strong neighbor in the same circle got promoted.

### Confirms the vendor's sample output exactly

Walking the new logic against the author's example page:
- `96372` (score `0.6986`) vs `90460` (score `0.5059`) vs `20600` (score `0.455`), AI said `circle_multiple` @ conf `0.62`, selected `96372`+`90460` → only `96372` clears `mark_multi_strong_score` (0.58) → **only `96372` promoted**, `90460` stays manual review. ✅ matches their sample exactly.
- `J3301` (0.7303) + `J1885` (0.728) + `J0696` (0.5317), AI said `circle_multiple` @ conf `0.55`, selected all three → `J3301` and `J1885` clear 0.58, `J0696` doesn't → **both `J3301` and `J1885` promoted**, `J0696` stays. ✅ matches exactly.
- `99214` promotion is unaffected — that's still the pre-existing `circle_single` + Office-Services-only path, untouched by this patch.

---

## 3. New/changed configuration

`.env.example` gained:
```
# Distinct adjacent multi-circle confirmation (procedure rows only)
CHARGESHEET_MARK_MULTI_PROMOTE_CONFIDENCE=0.55
CHARGESHEET_MARK_MULTI_STRONG_SCORE=0.58
```
Both have sane defaults baked into `settings.py`, so nothing breaks if left unset — but you get direct tuning knobs for how aggressive this new path is if it ever needs adjusting.

Version strings also changed (informational — these are what you saw differ in the two JSON payloads):
- `CHARGESHEET_PIPELINE_VERSION` default → `locked-candidates-ai-verified-v2.2.1-distinct-multicircle`
- `CHARGESHEET_MARK_RESOLVER_VERSION` default → `candidate_crop_resolver_v2_2_1_distinct_multicircle`

---

## 4. Regression to watch: our `CHARGESHEET_MATCH_MIN` override was dropped again

Exactly like every prior vendor drop (v2, v2.2.1), this fresh delivery's
`settings.py`/`template_registry.py` don't know about our
`CHARGESHEET_MATCH_MIN` env-var override — the `match_min_override` field and
its plumbing into `locked_template()` simply aren't present in this copy.
**If/when we integrate this**, we need to reapply the same small patch we've
applied twice before:

```python
# settings.py
def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else None
# ...
match_min_override: float | None
# ...
match_min_override=_optional_float("CHARGESHEET_MATCH_MIN"),
```
```python
# template_registry.py
@lru_cache(maxsize=1)
def locked_template() -> LockedTemplate:
    _, catalog, reference_path = load_manifest_and_catalog()
    s = get_settings()
    thresholds = {"match_min": s.match_min_override} if s.match_min_override is not None else None
    return LockedTemplate(catalog=catalog, reference_path=str(reference_path), thresholds=thresholds)
```
Not a defect in their delivery — just a reminder this is our own local patch that needs re-carrying-forward, same as last time.

---

## 5. New doc: `README_OUTPUT_AND_DISPLAY.md`

A genuinely useful, thorough (23-section) consumer-facing guide to the output
JSON contract — confirmed vs. manual-review vs. diagnostic-only fields, a
recommended front-end mapping function, a full "do/don't" list, and a
decision-matrix table. Worth keeping alongside our own
[response_explanation.md](response_explanation.md) (which goes deeper into
the raw geometry/AI internals) — this new doc is closer to a
frontend-integration spec, ours is closer to an engineering/debugging
reference. No code changes are implied by this file; it's documentation only.

One thing to note: it describes the `adjacent_code_overlap` reason as now
covering *both* the old single-circle path and the new multi-circle partial
path — accurately reflecting this delivery's actual behavior.

---

## 6. Test suite

Ran the vendor's own suite in place (`python -m unittest discover -s tests -v`):

```
Ran 31 tests in 1.108s
OK
```
27 tests carried over from v2.2.1 unchanged, plus **4 new tests** directly
covering this feature:
- `test_adjacent_distinct_multiple_promotes_only_strong_procedure_rows` — 2-of-3 promoted, weak one stays.
- `test_adjacent_distinct_multiple_can_promote_one_strong_row_only` — exactly the `96372`/`90460` scenario.
- `test_adjacent_distinct_multiple_does_not_promote_weak_lab_rows` — both weak → nothing promoted.
- `test_adjacent_distinct_multiple_never_promotes_diagnosis` — hard-codes the diagnosis exclusion, even with high scores/confidence.

---

## 7. Recommended next step

This is ready to integrate the same way v2.2.1 was: copy into a new
`v2_2_1_2_computer_vision/` (or patch the two changed files directly into our
existing `v2_2_1_computer_vision/`), reapply the `CHARGESHEET_MATCH_MIN`
override (§4), re-run the vendor test suite + our own live smoke test, then
swap `api.py`'s import over. Let me know which you'd like — a fresh sibling
package (safer, matches how we handled v1→v2→v2.2.1) or an in-place patch to
the current `v2_2_1_computer_vision/` (faster, smaller diff since only 2 files
actually changed).
