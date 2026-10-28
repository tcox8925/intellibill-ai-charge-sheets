# Charge-sheet v3 — Requirements & Setup

This package is **self-contained**. It does not import anything from
`chargesheet_extraction_v2`. Unzip it anywhere and run.

---

## 1. Runtime requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | tested on 3.11 and 3.12 |
| Poppler (`pdftoppm`) | any recent | **not a pip package** — see §3 |
| numpy | >=1.26,<3 | |
| opencv-python-headless | >=4.9,<5 | `opencv-python` also works |
| Pillow | >=10,<13 | used by the header/notes extractor |
| anthropic | >=0.64,<1 | not needed for `--no-ai` runs |
| azure-identity | >=1.17,<2 | **only** for the Key Vault credential path |
| azure-keyvault-secrets | >=4.8,<5 | **only** for the Key Vault credential path |

No database, no network services, no Azure resources are required if you supply
`ANTHROPIC_API_KEY` directly.

---

## 2. Install

```bat
cd C:\Users\poorn\PycharmProjects\EOB_v9\chargesheet_v3
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Poppler — the one non-pip dependency

`render.py` shells out to `pdftoppm`. The locked reference was calibrated
against this renderer at 200 DPI; the manifest enforces both, so a different
renderer is a template-version change, not a config tweak.

**Windows**
1. Download a build from `github.com/oschwartz10612/poppler-windows/releases`
2. Unzip, e.g. to `C:\poppler`
3. Add `C:\poppler\Library\bin` to **PATH**
4. Open a new terminal and confirm: `pdftoppm -v`

**macOS**: `brew install poppler`
**Debian/Ubuntu**: `sudo apt-get install poppler-utils`

If `pdftoppm` is missing, `render.py` raises with these instructions rather than
failing obscurely.

---

## 4. Credentials

Two paths; the first one found wins.

**A — API key (simplest, no Azure packages needed)**

```bat
set ANTHROPIC_API_KEY=sk-ant-...
```

For a Foundry endpoint, also set `ANTHROPIC_BASE_URL`.

**B — Azure Key Vault → Anthropic Foundry (default internal / EOB_v9 path)**

```bat
az login
```

Requires read access to secret `834-claude-key` in
`https://keyvault-834analytics.vault.azure.net/`. Same defaults as v2; the env
var names are unchanged, so existing deployment scripts keep working.

Neither is needed for `--no-ai`.

---

## 5. Verify before running

```bat
python preflight.py            :: everything except a live model call
python preflight.py --with-ai  :: also makes one tiny live call
```

Expected clean output:

```
[  OK  ] Python — 3.12.3
[  OK  ] cv2 — 4.13.0
[  OK  ] pdftoppm — /usr/bin/pdftoppm  pdftoppm version 24.02.0
[  OK  ] module settings ... module template_registry
[  OK  ] locked template — nwa_internal_medicine_superbill_locked_v1 v1,
         206 rows, reference 2122x1649, renderer dpi 200
All required checks passed.
```

Exit code 0 means the package can run. The two `azure.*` lines are WARN, not
FAIL, when you are using an API key.

---

## 6. Run

```bat
:: geometry only — no model calls, dumps every crop it would have sent
python run_v3.py July-02.pdf --pages 6,19,21 --no-ai --dump-crops .\crops

:: full pipeline
python run_v3.py July-02.pdf --pages 6,19,21 --out july02-v3.json

:: skip the recall sweep (roughly halves model calls)
python run_v3.py July-02.pdf --out full.json --no-audit
```

| Flag | Effect |
|---|---|
| `--pages 6,19,21` | 1-based page numbers; omit for the whole PDF |
| `--out FILE` | JSON output path |
| `--no-ai` | localize only; no credentials required |
| `--no-audit` | skip the full-page recall sweep |
| `--no-header` | skip header/notes extraction |
| `--refresh-header-cache` | ignore the cached header result for these pages |
| `--dump-crops DIR` | write crops + debug images |
| `--page-dir DIR` | persistent source/aligned page PNG directory; defaults to `<out-stem>_pages` |
| `--model NAME` | override the crop-reader model |

---

## 7. Files

```
run_v3.py              CLI and orchestration
alignment.py           page registration (ORB + ECC), locked catalog access
handwriting.py         clearance-gated handwriting isolation, glyph boxes
mark_localizer.py      mark grouping, write-in regions, candidates, geometry hint, crops
mark_adjudicator.py    crop-reader contract (index-only) + reconcile policy
page_audit.py          full-page recall sweep, coordinates-only
header_extract.py      header/notes extraction (unchanged from v2)
dates.py               header date normalisation (unchanged from v2)
render.py              pdftoppm wrapper with a Poppler-aware error
template_registry.py   manifest/checksum/policy verification, fail-closed
model_client.py        credential precedence; delegates shared internal auth
auth.py                EOB_v9-style DefaultAzureCredential -> Key Vault -> Foundry
settings.py            env-var configuration, all defaulted
preflight.py           environment checker
requirements.txt
README_V3.md           architecture rationale
REQUIREMENTS.md        this file
templates/nwa_internal_medicine_superbill_locked_v1/v1/
    manifest.json      checksums + renderer contract
    catalog.json       206 locked code cells
    reference.png      PHI-sanitised reference scan
```

`runtime/header_cache_v3/` is created on first run.

---

## 8. Configuration

Everything is defaulted. Override with environment variables if needed — names
carried over from v2:

| Variable | Default | Purpose |
|---|---|---|
| `CHARGESHEET_TEMPLATE_ID` | `nwa_internal_medicine_superbill_locked_v1` | which locked template |
| `CHARGESHEET_TEMPLATE_VERSION` | `1` | |
| `CHARGESHEET_TEMPLATE_ROOT` | `./templates` | template search root |
| `CHARGESHEET_RENDER_DPI` | `200` | **must match manifest** or the run fails closed |
| `CHARGESHEET_MARK_MODEL` | `claude-opus-4-6` | crop reader + recall sweep |
| `CHARGESHEET_HEADER_MODEL` | `claude-opus-4-6` | header/notes |
| `CHARGESHEET_ENABLE_HEADER_NOTES` | `true` | |
| `CHARGESHEET_ENABLE_PAGE_AUDIT` | `true` | |
| `CHARGESHEET_PROMOTE_CONFIDENCE` | `0.75` | visual confidence to confirm |
| `CHARGESHEET_HINT_MARGIN` | `0.12` | diagnostic-only geometry margin; does not affect ownership |
| `CHARGESHEET_MIN_PHYSICAL_COVERAGE` | `0.50` | adjacent code-box coverage required for confirmation |
| `ANTHROPIC_API_KEY` | — | credential path A |
| `ANTHROPIC_BASE_URL` | — | Foundry endpoint for path A |
| `AZURE_KEY_VAULT_URL` | `https://keyvault-834analytics.vault.azure.net/` | path B |
| `ANTHROPIC_KEY_SECRET` | `834-claude-key` | path B |
| `ANTHROPIC_FOUNDRY_ENDPOINT` | `https://sql-test-resource.services.ai.azure.com/anthropic/` | path B |

Tune in the order given in `README_V3.md` §Tuning — `clearance_min` first,
`bridge_kernel` second, thresholds last and only against a regression set.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'dates'` | running the old non-standalone v3 files outside the v2 folder | this package has no such dependency — run `run_v3.py` from *this* directory |
| `pdftoppm not found on PATH` | Poppler not installed / PATH not refreshed | §3, then open a **new** terminal |
| `CHARGESHEET_RENDER_DPI=X does not match locked template DPI=200` | DPI override set | unset it, or recalibrate the template |
| `Locked template ... checksum mismatch` | catalog.json or reference.png edited | restore from the zip; edits require regenerating manifest checksums |
| `Could not initialize the Anthropic client` | no key and no `az login` | §4 |
| `template_match_below_threshold` flag | page is not this form, or scan quality too poor | check `template_match.score` (expect 0.92+) |
| All pages report `marks=0` | page wasn't registered, or scan is very clean | run `--no-ai --dump-crops` and open `handwriting.png` first |

`--dump-crops` is the debugging surface. Per page it writes `aligned.png`,
`handwriting.png` (the isolated layer — look here first), and
`mark-NN.png` / `mark-NN.json` for every mark with its candidate list and
geometry hint. A missed code is always either "the localizer made no mark" or
"the reader misread the crop", and those files tell you which.

---

## 10. Verified behaviour

Localizer, no model calls, on `July-02.pdf`:

```
page  1   4 marks in table band   99214 · E11.65 · 93923+I70.213 · 83036
page  6   1 mark                  99395   (hint 0.988 vs 99215 0.702)
page 19   1 mark + 2 margin marks with no locked row in range
page 21   1 mark                  99214   (hint 0.925 vs 99205 0.803)
```

Full path, exercised with a stubbed reader:

* reader selects the geometrically-supported row → **confirmed**
* reader confidently selects a locked row → **confirmed**, even when the
  diagnostic geometry hint ranks a neighboring row higher
* one complete loop visibly overlaps multiple locked rows → **all physically overlapped/enclosed rows confirmed**
* only incomplete/faint/unclear circle-like evidence → **manual_review**; geometry never forces a winner
* marks with no locked row in range → **rejected_marks**, reason
  `no_locked_row_within_range`, with no model call spent


## v3.3 output policy note

A physically real incomplete circle/arc is not automatically manual review. If the visible arc clearly has one dominant row, that row is confirmed. If the visible arc physically spans multiple rows with no clear majority, all physically affected locked rows are confirmed. Nonphysical residual/template artifacts confirm nothing. `procedure_codes` remains confirmed procedures only.
