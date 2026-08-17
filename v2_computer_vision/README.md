# Charge-sheet extraction — clean production package

This folder contains only the extraction pipeline, the approved locked template, tests, and the minimal PostgreSQL schema. It contains **no worker, Blob polling, Docker, API, deployment, or queue code**.

## Runtime rule

Billing-code selection is deterministic:

```text
PDF page
  -> render at 200 DPI with pdftoppm
  -> align to approved locked template
  -> if template does not match: return zero codes (fail closed)
  -> inspect partial-arc/ring evidence around every fixed code coordinate
  -> tolerate incomplete, split, and grid/text-overlapping colored circles
  -> when alignment >= 0.90, do the same for black ink by template subtraction
  -> resolve clear code winners deterministically
  -> put genuinely ambiguous marks in page.possible_marks (same output JSON)
  -> output only confirmed CPT/HCPCS/ICD codes as selections
```

The AI model is used **only** for variable header/payment fields and handwritten clinical notes. It cannot add, remove, rename, or classify selected billing codes. Ambiguous code marks never become confirmed selections automatically; they remain in `possible_marks` with review coordinates and page flags such as `circle_review_required`.

## Folder

```text
run.py                    main PDF extractor
locked_template.py        deterministic alignment + circle geometry
extract.py                header/payment/clinical-note model extraction only
model_client.py           minimal model authentication
render.py                 locked pdftoppm renderer
settings.py               local runtime settings
dates.py                  date normalization
template_registry.py      verifies template checksums/policy
verify_template.py        geometry-only verification; no AI call
requirements.txt
migrations/001_chargesheet.sql
templates/
tests/
runtime/header_cache/     stable header/notes cache
```

## Install

Python packages:

```bat
python -m pip install -r requirements.txt
```

`pdftoppm` (Poppler) must also be available on PATH because the locked template was calibrated using that renderer.

## Credentials for header/notes

Either set `ANTHROPIC_API_KEY` (plus `ANTHROPIC_BASE_URL` when needed), or use Azure Key Vault + Foundry by setting:

```text
AZURE_KEY_VAULT_URL
ANTHROPIC_KEY_SECRET
ANTHROPIC_FOUNDRY_ENDPOINT
```

With the Azure option, authenticate locally with `az login`. No credentials are required for `--no-ai` or `verify_template.py`.

## Verify the package

```bat
python -m unittest discover -s tests -v
python verify_template.py BRN3C2AF4B84200_004495.pdf --pages 1,4,5,11
```

The test PDF is intentionally not bundled; place your test PDF beside the scripts or pass its full path.

## Extract

Full PDF:

```bat
python run.py BRN3C2AF4B84200_004495.pdf --out results.json
```

Selected pages:

```bat
python run.py BRN3C2AF4B84200_004495.pdf --pages 1,4,5,11 --out spotcheck.json
```

Geometry only, with no AI call:

```bat
python run.py BRN3C2AF4B84200_004495.pdf --no-ai --out geometry_only.json
```

Header/notes are cached by rendered-page SHA-256 + model + extractor version. Re-running the same page therefore reuses the same transcription. To intentionally re-run the model:

```bat
python run.py BRN3C2AF4B84200_004495.pdf --refresh-header-cache --out results.json
```

## Database objects

Run `migrations/001_chargesheet.sql`. It creates exactly five tables plus one convenience view:

1. `wpo.chargesheet_templates` — approved template/version and immutable catalog metadata.
2. `wpo.chargesheet_documents` — one source PDF and its SHA-256.
3. `wpo.chargesheet_pages` — one current extraction row per PDF page, including header, template match, flags, and geometry evidence.
4. `wpo.chargesheet_selections` — confirmed circles only; procedure/diagnosis code + geometry source. Ambiguous `possible_marks` remain in the page `raw_result` JSON and never enter this table.
5. `wpo.chargesheet_notes` — transcribed clinical notes only.

View: `wpo.vw_chargesheet_page_output` provides procedure and diagnosis arrays per page.

There are intentionally no `runs`, `feedback`, worker-queue, or deployment tables in this package.
