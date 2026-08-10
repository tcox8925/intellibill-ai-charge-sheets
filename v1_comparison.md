# Chargesheet Extraction: Current Repo vs. `chargesheet_extraction_v1`

**Repo A (current, in production use):** `/Users/srinivasbodduru/Downloads/chargesheet`
**Repo B (candidate new version):** `/Users/srinivasbodduru/Downloads/chargesheet_extraction_v1`

## What the new folder actually is

It's **not an incremental update to the same approach** — it's a fundamentally different extraction strategy for one specific form (the NWA Internal Medicine superbill):

- **Repo A (current):** the LLM *is* the code-selection engine. The vision model looks at the page plus an injected catalog of codes and decides which CPT/ICD codes are circled (`extract.py`), with `mark_detect.py` giving it a supporting ink-isolated image as extra evidence. Multi-template support via content-fingerprint scoring (`fingerprint.py`) — any new form auto-builds a catalog (`build_catalog.py`).
- **Repo B (v1 folder):** the LLM is **banned** from selecting codes at all. Code selection is 100% deterministic computer vision (`locked_template.py`) — a page gets registered pixel-for-pixel against one fixed reference image, then circles are found by fixed-coordinate geometry (color-blob detection + residual-ink detection). The LLM (`extract.py` in Repo B) is used only for header fields and free-text notes; its system prompt explicitly forbids it from ever mentioning codes, and any code-like keys are defensively stripped from its output even if it disobeys. If a page doesn't register well against the reference image, the pipeline fails closed (returns an empty result) rather than guessing.

Repo B only supports **one locked template** (no catalog auto-building, no multi-form registry), has no DB/blob-storage/FastAPI/background-task code at all (it's CLI-only), needs a new system dependency (`pdftoppm`/Poppler — not just a Python package), and its catalog file format (pixel coordinates) is incompatible with Repo A's catalog format (flat code lists per section). It does include two things Repo A has zero of: real automated tests, and a from-scratch Postgres schema shaped around its own result fields.

## Potential improvements it offers

1. **Deterministic, hallucination-proof code selection** for a known form — the same input pixels always yield the same codes; removes the whole class of LLM guessing/inconsistency risk for that form.
2. **Fail-closed safety enforced in code** — checksum-verified template artifacts, and a "safety policy" (`ai_can_add_codes: false`, `circle_only`, `unknown_template: fail_closed`) that's actively checked at load time, not just documented.
3. **Real regression tests** — `tests/test_contract.py` (artifact hashes, safety policy, catalog integrity) and `tests/test_fail_closed.py` (regression test for the fail-closed safety behavior). Repo A has no equivalent tests anywhere.
4. **Centralized, typed settings module** (`settings.py`, an `lru_cache`d frozen dataclass) vs. Repo A's scattered `os.environ.get(...)` calls spread across `extract.py`, `run.py`, `mark_detect.py`, `build_catalog.py`.
5. **Response caching by content hash** — header/notes LLM responses are cached on disk keyed by page SHA-256 + model + extractor version, avoiding redundant (and costly) LLM calls when re-running the same page. Repo A re-calls the LLM every time, even for identical bytes.
6. **Self-contained model client** (`model_client.py`) — implements its own KV → Foundry fallback directly, rather than Repo A's `run.make_client()`, which does fragile `sys.path` searching hoping a sibling `auth.py` module exists nearby.
7. **More rigorous false-positive handling for circle detection** — alignment-quality gating (the black/residual-ink detection path disables itself below a stricter registration-confidence threshold) and explicit adjacent-cell/adjacent-row conflict resolution, which Repo A's `mark_detect.py` leaves entirely to the LLM's visual judgment.
8. **In-memory page rendering** — pages are returned as in-memory `(page_number, png_bytes, sha256)` objects from a temp directory, with nothing persisted to disk unless explicitly needed. Repo A's `pdf_raster.py` always writes page PNGs to a persistent `pages_dir`.
9. **Renderer/template contract pinning** — the manifest explicitly records and checks the exact rasterizer engine + DPI (`pdftoppm` at 200 DPI) the locked template was calibrated against, so a renderer change is caught as a template-version mismatch rather than silently producing bad geometry. Repo A's DPI is just a documented constant (`RENDER_DPI = 200` in `run.py`) with no enforcement.

## Why this isn't a drop-in swap

Adopting the locked-template approach for the NWA form would require: a dispatch layer to route pages to the right extraction method, reconciling two incompatible catalog schemas (geometry vs. flat code lists), adding `pdftoppm`/Poppler as a new system dependency, a real Postgres migration, aligning flag-string vocabularies (`extraction_flags.py`) between the two pipelines, and moving Repo B's local-disk header cache to something durable for a multi-instance API deployment. It's a multi-step project, not a single edit — full risk/mismatch breakdown available on request if/when integration work starts.

**Status:** analysis only, no code changes made. Revisit when ready to scope either (a) the low-risk portable improvements (settings pattern, tests, caching) or (b) the full locked-template integration for the NWA form.
