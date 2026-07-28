# Charge-sheet extraction — local prototype

Vision-first pipeline for the **NWA Internal Medicine superbill** (scanned,
handwritten). Unlike EOB v9, OCR text is useless here (the PDF has *zero* font
layer), and the printed grid is identical on every page — so **vision is
primary** and the **template is extracted once**, not per page.

## Files
- `catalogues/catalog.json` — the template catalog (14 sections, ~200 codes, CPT + ICD-10).
  Built **once per template**, hand-verified. `needs_verify: true` marks the 6
  cells partly obscured/ambiguous on the sample scan — confirm before production.
- `extract.py` — per-page: rotate upright → inject catalog → one Opus vision
  call → strict JSON → **validate every code against the catalog** (drops any
  hallucinated code).
- `fingerprint.py` — template identity by *content* (anchor labels + codes), not
  pixel position. Outcomes: `known` (reuse catalog) / `unknown` (new-template
  queue) / `fuzzy` (human decides). Starts strict.
- `run.py` — split PDF → extract each page → differentiate pages by name/DOB,
  flag blanks & duplicates → write `results.json`.
- `results.json` — **demo**: real extraction of pages 1, 8, 14.

## Run
```
pip install anthropic pymupdf pillow
export ANTHROPIC_API_KEY=...
python run.py june-30.pdf                 # all pages
python run.py june-30.pdf --pages 1,8,14  # just these
```

## What each result gives you (per page)
- `header` — date, name, dob, prn, insurance, copay, amount_paid, payment_type
- `circled_procedures` — selected CPT/HCPCS (with mark type + confidence)
- `circled_diagnoses` — selected ICD-10
- `notes` — handwritten margin text; off-form handwritten codes are captured
  under `handwritten_code_not_on_form` (e.g. page 8's `M25.561 R Knee Pain`)
- `flags` + `confidence` — feed a human review queue rather than trusting blind

## Known hard cases (why confidence/flags are first-class)
Multi-color ink, circle vs check vs strikethrough, marks that clip an adjacent
row, and margin notes referencing codes not on the form. The prototype surfaces
these instead of guessing.

## Next (when you're happy with results)
Move catalog + results to `wpo.*` with `template_id`+`version` (mirrors the
text-to-SQL admin catalog: background discovery, hot-reload, versioning), and
swap the client for your KV/VNet-backed deployment like `pch-eob-pipeline`.
