# v2.2.1 delivery review — action plan

Analysis only — nothing integrated yet. `chargesheet_extraction_v2_2_1` has not been copied
into the repo.

## Does it fix the bug we reported?

**Partially, by deliberate design — not the way their reply email described.**

Their reply said they'd update `_resolve_candidates()` to use "actual mark geometry/centroid/
overlap" to pick a winner deterministically. What they actually shipped instead:

- `_resolve_candidates()` was changed to **stop trying to auto-resolve** the "both candidates
  independently confirmed" case at all — it now always defers to `possible_marks` (new test:
  `test_two_confirmed_centered_adjacent_candidates_remain_ambiguous`, which locks this in as
  intended behavior, not a bug).
- Instead, a **new AI-based post-processing step** (`mark_resolver.py`) reviews those
  `possible_marks` groups and can *promote* one into a confirmed selection — but only under a
  specific gate: **`adjacent_code_overlap` groups can only be AI-promoted if every candidate code
  in the group is in the "Office Services" catalog section.**

I checked our two actual reported cases against the real catalog:

| Case | Section | AI-promotable? |
|---|---|---|
| `99215` vs `99395` | Office Services | **Yes** |
| `F11.20` vs `36415` | Miscellaneous | **No — permanently manual-review by design** |

So: our E/M-code case (`99395`) can now resolve automatically via the AI reviewer. Our
lab/misc-code case (`36415`) will **never** auto-confirm under this design — it always lands in
a new `manual_review_procedures` bucket instead. That's a deliberate, conservative product
choice on their part ("preserve the baseline's useful office-code recovery without auto-billing
Misc/Labs adjacency" — their code comment), not an oversight, but it means the fix only covers
half of what we reported, and it's worth knowing that going in rather than assuming both cases
are resolved.

## What's genuinely new (beyond the resolver fix)

This delivery is much larger in scope than the bug we filed:

1. **`mark_resolver.py`** — sends a cropped, redacted image collage (candidate cells only,
   labeled A/B/C, no code text visible) to an Anthropic vision model, asking it to classify each
   crop as `circle_single`/`circle_multiple`/`scribble`/`ambiguous`/`none`. The model never sees
   or outputs a billing code — it only picks a letter, which the code maps back to a
   *pre-existing* deterministic candidate. It cannot invent a code outside what
   `locked_template.py` already flagged as physical ink at a locked coordinate.
2. **`manual_review_procedures`/`manual_review_diagnoses`** — a new, separate output bucket for
   codes that have real geometric evidence but weren't safely auto-confirmed. This is a genuine
   UX improvement over dumping everything into one generic `possible_marks` blob — a reviewer now
   sees an actionable, code-specific manual-review item.
3. **"Procedure should never silently be zero" invariant** — if a page has no confirmed
   procedure at all, the strongest procedure candidate group (preferring Office Services) is
   automatically surfaced into `manual_review_procedures` rather than the page just reporting
   zero procedures.
4. **Diagnosis auto-confirmation got stricter** — a residual-geometry diagnosis candidate with
   `geometry_mode: partial_arc` (sector_coverage < 11) is now **never** auto-confirmed regardless
   of score; it's demoted to a new `possible_marks` reason (`diagnosis_partial_arc_not_confirmed`)
   for manual review. Procedures are unaffected.
5. **`runtime/mark_cache/`** — caches AI mark-review results by page-sha + possible_marks
   fingerprint + model + resolver version, same caching pattern as the existing header/notes
   cache.
6. **`auth.py`** (new, 49 lines) — an alternate Key Vault + Anthropic Foundry auth path,
   orthogonal to circle detection. Tied to a separate `Charge_Sheet_Authentication_Replacement_
   Guide.docx` we haven't reviewed. Not required — we already have working auth via our own
   `model_client.py`/`.env`.

Test suite grew from 11 to **27 tests, all passing** (verified by running it myself), including
mocked-AI-client tests specifically for the new resolver's promotion/suppression rules.

## Concerns to weigh before adopting

- **Real regression risk for a page we already saw:** our real production page's confirmed
  diagnosis `R56.9` was `residual_geometry` with `sector_coverage=9` (< 11) — under v2.2.1's new
  diagnosis rule, that same page would now demote `R56.9` to manual review instead of confirming
  it. That's a behavior change affecting more than just the bug we filed, and it isn't mentioned
  anywhere in their reply.
- **AI is now in the code-selection path, even if constrained.** The safeguards are genuinely
  well thought out (redacted crops, letter-only output, promotion requires the AI-selected
  candidate to *independently* still clear the deterministic confirm threshold, `possible_marks`
  is the fallback on any doubt) — but this is a philosophy shift from "100% deterministic
  billing-code selection, AI only for header/notes" to "AI can promote a candidate the geometry
  couldn't safely resolve alone." Worth a deliberate go/no-go decision, not a silent swap.
- **Extra cost/latency/failure-mode per document.** Any page with `possible_marks` now makes an
  additional real Anthropic API call (cached after the first run, same as header/notes).
- **Output schema grew substantially** (`manual_review_procedures`, `manual_review_diagnoses`,
  `procedure_codes`, `procedure_summary`, `suppressed_marks`, `mark_ai`, a `safety` block, and a
  renamed flag: `circle_review_required` → `mark_review_required`, `locked_coordinate_selection`
  → `locked_coordinate_candidate_source`). Our `pipeline_adapter.py` wrapper and `db.persist_page_v2`
  don't know about any of this yet — integrating means deciding what (if anything) to persist from
  the new fields, not just swapping an import.
- **Our own `CHARGESHEET_MATCH_MIN` override is gone again** — same as the v1→v2 transition,
  this fresh vendor drop doesn't carry our settings patch forward. Needs reapplying (same
  one-line pattern as before) or this reverts to the stricter `0.78` default.
- **Documentation gaps:** README references `CHANGELOG_V2_2_1.md`, which isn't in the delivered
  files. The 1091-line tuning guide's table of contents is unchanged from the prior version —
  none of `mark_resolver.py`, `manual_review_*`, or the new invariants are documented there.
- Hardcoded default endpoints in `settings.py`/`auth.py` (`keyvault-834analytics.vault.azure.net`,
  a `sql-test-resource...` Foundry endpoint) resemble real infrastructure naming — worth
  confirming with the vendor what these actually point to before relying on the defaults.

## Recommended action plan

1. **Go back to the vendor** on two open items: (a) the missing `CHANGELOG_V2_2_1.md`, and (b)
   explicit confirmation of the `R56.9`-style diagnosis-demotion behavior change — was that
   intentional scope, and is it expected to affect real pages beyond the one we reported?
2. **Decide, deliberately, whether AI-assisted mark promotion is acceptable** for this pipeline's
   safety posture — this is a product/compliance call, not just an engineering one, given the
   "AI never selects billing codes" principle this whole project was built around.
3. If yes: copy this into the repo as `v2_computer_vision`'s replacement (or a new
   `v2_2_1_computer_vision/`, keeping the current one side-by-side per our usual pattern),
   reapply the `CHARGESHEET_MATCH_MIN` override, update `pipeline_adapter.py` for the new
   `detect()`/output shape, and decide what the new schema fields mean for `db.py`.
4. Either way, reprocess the exact page we reported (`page 1` of `1553326257-1553326257001_
   2026-07-07_june-30.pdf`) against this version once integrated, to confirm `99395` actually
   promotes and `36415` correctly lands in `manual_review_procedures` — don't assume the vendor's
   own tests cover our exact real-world page.
