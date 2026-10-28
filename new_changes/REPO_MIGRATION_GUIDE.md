# Repo Migration Guide — shared database tables for external/logic repos

## Who this is for

Any separate repo/service (a different deployment, possibly a different
language/stack entirely) that shares this repo's Postgres database and reads
or writes rows directly into its tables, rather than going through an API
this repo exposes. This is **not** a guide to this repo's own internal
schema conventions in general — it's specifically the contract those outside
writers need to follow, kept up to date as this repo's schema evolves out
from under them.

**Known writers today:**

- **Chargesheet extraction service** (separate repo/deployment — currently
  `CHARGE_SHEET_INGEST_URL_PROD`, an Azure App Service reached via
  `POST /chargesheet/ingest {blob_path}`) — writes into `"EDI_Tebra".attachments`.
- **The external EOB extraction pipeline** — writes into `"EDI_Tebra".eob_deposits`/
  `eob_postings` after this repo triggers it. See the dedicated section below;
  this integration is less mature than the chargesheet one (no callback route
  exists yet for it to also write `attachments` directly — tracked in
  `DOCUMENT_MANAGEMENT_REMITTANCE_EOB_MERGE_PLAN.md`).
- **Tebra RPA** — technically a separate deployment (a browser-automation
  client), but it only ever calls this repo's own `medicalExtraction.uploadMedicalFile`
  tRPC mutation over HTTP; it never touches the database directly, so none
  of this doc's direct-write rules apply to it. Mentioned here only so it's
  not confused with the other two, which do write directly.

If your service is about to start sharing a table with this repo for the
first time, this doc is the template to extend — add your table/columns to
the "Current live schema" and "Full column reference" sections, and add
yourself to "Known writers" above.

## How to use this doc

- **Integrating for the first time?** Read "Current live schema," "Full
  column reference," and the numbered Rules for whichever table you're
  writing to. Skip the changelog at the bottom unless you want the history.
- **Already integrated, and something in your writes stopped working (rows
  invisible, a query throws, a downstream job silently skips your rows)?**
  Read "What changed and what you need to do" at the bottom — it's a
  chronological log of every schema/behavior change that affected an
  external writer, each with the specific action that change required. Find
  the entries after whatever date you last synced against this doc.

## How the pieces fit together (general shape)

Two different integration shapes exist in this repo today, depending on the
table:

1. **One row per document, no parent/child** (Tebra/Practice Fusion/PF
   Facesheet's "Facesheet" flow, and the Paper EOB flow) — each uploaded
   document gets exactly one `attachments` row, and everything about it
   (extraction result, scope, claim linkage) lives on that same row.
2. **One parent row, many child rows** (the chargesheet flow specifically) —
   Document Management inserts **one parent row** first
   (`attachment_type = 'Paper Charge Sheet'`, `parent_attachment_id IS NULL`,
   `status = 'T'`) and uploads the blob; the external service then splits
   that PDF into per-claim pages and **inserts one child row per split page**
   (`parent_attachment_id` = the parent's `id`), writing OCR/extraction
   results onto each child. This shape is specific to chargesheet
   processing — don't assume it for a new integration unless your data
   actually splits into multiple documents from one upload the same way.

Either shape, this repo's own downstream code
(`createClaimsFromProcessedDocumentAttachments.ts`,
`professionalClaimCreationHelper.ts`, `medicalExtractionWorkflowService.ts`)
later reads whatever extraction output your service wrote, creates
`claim_header` rows from it, and writes `type_id` (→ the new claim's id) /
`associated_claim_dcn` / `claim_creation_response` / `status = 'C'` back onto
your row(s) once a claim exists. **No external writer should ever set those
itself** — they belong to this repo's claim-creation step, which always runs
strictly after your write, whichever table you're writing to.

## Current live schema — `"EDI_Tebra".attachments`

Cross-checked against the Drizzle model (`claimAttachments` in
`server/src/db/new_schema.ts`) and every migration through `00139`, current
as of 2026-09-07. Treat the semantic write-up below ("Full column reference"
and the rules that follow) as commentary on top of this; treat this block as
the source of truth for types/nullability/defaults if the two ever disagree.

```
Column                          Type                          Nullable  Default
id                               integer                       NO        nextval('"EDI_Tebra".claim_attachments_id_seq')
type_id                          uuid                          YES       —
clm_att_path                     text                          YES       —
clm_att_filename                 text                          YES       —
clm_att_datetime                 timestamp without time zone    YES       —
clm_login                        varchar(500)                  YES       —
created_at                       timestamp without time zone    NO        — (no default — must be supplied)
updated_at                       timestamp without time zone    NO        — (no default — must be supplied)
attachment_type                  varchar(50)                   YES       'claim' (don't rely on this — always set it explicitly)
user_id                          uuid                          YES       —
assigned_to_id                   uuid                          YES       —
status                           varchar(5)                    NO        'G'
raw_extracted_data               jsonb                         YES       —
processed_extracted_data         jsonb                         YES       —
processed                        boolean                       NO        false
sha                              varchar(64)                   YES       —
extraction_metadata              jsonb                         YES       —
parent_attachment_id             integer                       YES       — (FK → attachments.id, self-referencing — see below)
parent_attachment_name           text                          YES       —
original_file_name               text                          YES       —
client_id                        integer                       YES       — (FK → client.client_id)
group_id                         integer                       YES       — (FK → "group".id)
practice_id                      integer                       YES       — (FK → practice.id)
page_count                       integer                       YES       —
extracted_files_count            integer                       YES       —
rotation_degrees                 integer                       YES       —
upload_source                    text                          YES       —
associated_claim_dcn             varchar(100)                  YES       —
claim_creation_response          jsonb                         YES       —
retrieval                        text                          YES       —
document_dcn                     varchar(100)                  YES       — (filled in by a BEFORE INSERT trigger — see below)
associated_patient_id            integer                       YES       — (FK → medical_extraction_patients.id — Facesheet flow only)
associated_claim_id              uuid                          YES       —
ocr_text                         text                          YES       — (Facesheet flow only)
rpa_appointment_id               varchar(500)                  YES       — (Facesheet flow only)
retry_count                      integer                       NO        0
sftp_file_path                   text                          YES       — (Facesheet flow only)
json_manifest_entry              jsonb                         YES       — (Facesheet flow only)
associated_patient_header_id     uuid                          YES       — (FK → patient_header.patient_header_id — Facesheet flow only)
eob_deposit_id                   integer                       YES       — (FK → eob_deposits.id — added 2026-09-04, migration 00138)
payer_name                       text                          YES       — (added 2026-09-04, migration 00138)
deposit_amount                   numeric(12,2)                 YES       — (added 2026-09-04, migration 00138)
deposit_date                     date                          YES       — (added 2026-09-04, migration 00138)
billing_provider_npi             text                          YES       — (added 2026-09-04, migration 00138)
category                         varchar(20)                   YES       — ← populate this on every insert (see Rule 3)
is_archived                      boolean                       YES       — ← populate this on every insert (see Rule 3)
processing_status                varchar(20)                   YES       — (Facesheet flow only, added 2026-09-04, migration 00139 — not relevant to any current external writer, listed for completeness)
```

**Constraints/indexes that affect writers:**

- `parent_attachment_id` is a **real foreign key** to `attachments.id`
  (`attachments_parent_attachment_id_attachments_id_fk`) — for the
  parent/child shape, the parent row must exist (i.e. be committed) before
  you insert a child referencing it.
- `uq_attachments_facesheet_clm_att_path` — a unique index on `clm_att_path`,
  scoped `WHERE attachment_type = 'Facesheet'` only. There is currently no
  DB-level uniqueness constraint stopping a duplicate row from being
  inserted twice with the same `clm_att_path` for any other
  `attachment_type` — if your service can re-run/retry on the same blob,
  dedupe on your own side.
- `uq_attachments_facesheet_practice_fusion_dedup` — Practice Fusion's
  Facesheet flow only, not relevant to any current external writer.
- `idx_attachments_category` / `idx_attachments_is_archived` — plain btree
  indexes already exist on both, so populating them on your inserts is
  index-backed from day one, not just a bare column write.
- `idx_attachments_group_id` / `idx_attachments_practice_id` /
  `idx_attachments_client_id` — added 2026-09-04 (migration `00137`). Not
  relevant to your insert values, but worth knowing: these had no index at
  all before, so every Document Management dashboard query used to scan
  every client's rows, not just the requested group's.
- `tr_generate_document_dcn` — the `BEFORE INSERT` trigger that fills in
  `document_dcn` (confirms the rule below: don't set it yourself).

## Full column reference

| Column | Type | Notes |
|---|---|---|
| `id` | serial PK | — |
| `document_dcn` | varchar(100) | **Auto-generated by a `BEFORE INSERT` trigger** (`fn_generate_document_dcn_trigger`, migration `00110`). Do not set it — leave it out of your INSERT entirely. If you ever retry a failed insert with the same row object, make sure `document_dcn` isn't already populated from a prior attempt, since the trigger only fills it in when it's `NULL`. |
| `type_id` | uuid | Set later by this repo's claim-creation step (→ the created claim/patient id, depending on `attachment_type`). Leave `NULL` on insert regardless of which table/flow you're writing. |
| `associated_claim_dcn` | varchar(100) | Set later, by this repo's claim-creation step. Leave `NULL` on insert. |
| `claim_creation_response` | jsonb | Same — this repo only, after a claim is created/fails. Leave `NULL`. |
| `parent_attachment_id` | integer | Only meaningful for the parent/child shape (chargesheet flow). For a child row: the parent's `id`. `NULL` for every standalone-row flow (Facesheet, Paper EOB) and for the parent itself. |
| `parent_attachment_name` | text | The parent's `original_file_name`, denormalized onto the child for display. Only set when you set `parent_attachment_id`. |
| `original_file_name` | text | Human-facing file name. |
| `retrieval` | text | Set to `"API"` by the existing upload path; most external writers don't need to touch this. |
| `upload_source` | text | Free-text provenance tag identifying which flow/service wrote the row (existing values include `'practice_fusion'`, `'remittance_eob'`, `'tebra'`; Document Management's own manual uploads don't set it). Recommended: set this to a tag identifying your service. |
| `rotation_degrees` | integer, default `0` | Page rotation applied/needed. |
| `page_count` | integer | Set on the row the count applies to. |
| `extracted_files_count` | integer | For the parent/child shape: how many child rows this parent produced. |
| `client_id` / `group_id` / `practice_id` | integer, FK | The scope this document belongs to. For the parent/child shape, copy straight from the parent — same client/group/practice for every child. |
| `processed` | boolean, default `false`, **NOT NULL** | Flip to `true` once `processed_extracted_data` is written for that row. Downstream code (and the dashboard) treats `processed=false` as "still being worked on." |
| `sha` | varchar(64) | SHA-256 of the file content, hex. Not required unless you want per-file integrity checks. |
| `clm_att_path` | text | **Must be a bare Azure blob path, never a full URL.** See Rule 1 below — this is the single most important rule in this doc. |
| `clm_att_filename` | text | Blob's file name (just the name, not the path). |
| `clm_att_datetime` | timestamp | When the file was written to blob storage. |
| `clm_login` | varchar(500) | Who/what performed the write. Existing convention is a human name/login for user uploads, `'system'` for automated writers — use `'system'` (or a name identifying your service) rather than leaving it `NULL`. |
| `status` | varchar(5), default `'G'`, **NOT NULL** | See status vocabulary below. |
| `attachment_type` | varchar(50) | **Must exactly match one of the canonical values (Title Case)** — see Rule 2. Several places in this repo do a case-sensitive `=` comparison, not a case-insensitive one. |
| `raw_extracted_data` | jsonb | Raw OCR/AI output. Shape is flow-specific — e.g. the chargesheet flow's own read code (`buildProcessedExtractedData` in `documentManagementRouter.ts`) expects roughly `{ pages: [{ procedure_codes: [...], circled_diagnoses: [...], ... }], source_pdf: "..." }`. If you're a new integration, this column's shape is yours to define — just document it for whoever writes the corresponding read path. |
| `processed_extracted_data` | jsonb | Normalized/derived extraction result this repo's claim-creation step consumes directly. Shape is likewise flow-specific. |
| `extraction_metadata` | jsonb | Free-form metadata about the extraction run itself (confidence, model version, timing, etc.) — not read by any downstream logic today, so its shape is entirely up to you. |
| `user_id` | uuid | The uploading/triggering user's id, where one exists. For a fully system-generated row (no human uploader), leave `NULL` — this already occurs elsewhere in this codebase for automated writers. |
| `assigned_to_id` | uuid, FK → `users.id` | Who the document is queued to in Document Management. Not something an external writer should set — leave `NULL` on new rows; assignment happens separately, inside this repo. |
| `category` | varchar(20), nullable | **Required. See Rule 3.** |
| `is_archived` | boolean, nullable | **Required. See Rule 3.** |
| `eob_deposit_id` / `payer_name` / `deposit_amount` / `deposit_date` / `billing_provider_npi` | see schema block above | Paper-EOB-flow-only linkage/display columns (migration `00138`). Not written by any external writer today — this repo's own code sets them once an uploaded EOB is matched to an `eob_deposits` row. Listed here so the EOB pipeline (see below) knows what these are once that integration is built out further. |
| `processing_status` | varchar(20), nullable | Facesheet-flow-only (migration `00139`), internal to this repo's own async upload handling. Not relevant to any current external writer. |
| `created_at` / `updated_at` | timestamp, **NOT NULL, no DB default** | You must supply both explicitly on every insert (and bump `updated_at` on every update) — the column has no `DEFAULT now()`, so an insert that omits it will fail. |

Columns intentionally **not** listed above with per-writer instructions
because they belong to one specific in-repo flow and no external writer
should ever populate them: `associated_patient_id`, `ocr_text`,
`rpa_appointment_id`, `retry_count`, `sftp_file_path`, `json_manifest_entry`,
`associated_patient_header_id` (all Facesheet-only), `processing_status`
(Facesheet-only).

## Rule 1 — `clm_att_path` must be a bare blob path, never a full URL

Every read/write entry point in `azureBlobAttachmentManager.ts`
(`downloadFile`, `deleteFile`, `getPresignedDownloadUrl`, `archiveFile`,
`fileExists`) passes `clm_att_path` straight into the Azure SDK's
`getBlobClient(blobName)` / `getBlockBlobClient(blobName)`, which treats the
argument as a **literal blob name**, not something to parse as a URL. A row
whose `clm_att_path` is a full `https://<account>.blob.core.windows.net/<container>/<path>`
URL makes Azure look for a blob whose name is that entire URL string, and
every one of those operations fails with `The specified blob does not
exist.` — this exact bug hit ~1248 rows from an earlier, unrelated migration
into this table and had to be repaired with a one-time script. Store only
the path portion (e.g. `SomeClient-123/Exchange/Documents/Claim files/2026-09-02/file.pdf`),
never the scheme/host/container prefix.

## Rule 2 — `attachment_type` casing must match exactly

The canonical values, used verbatim (Title Case) everywhere in this repo,
are:

- `"Claim CSV File"`
- `"Paper Charge Sheet"`
- `"Facesheet"`
- `"Paper EOB"`
- `"EOB"`

Use whichever one describes your document type — copy it from a parent row
if one exists, rather than hardcoding a differently-cased variant. This
matters because several places do an exact, case-sensitive comparison
against these exact strings, not a case-insensitive one — e.g.
`eq(claimAttachments.attachmentType, "Paper Charge Sheet")` in
`resetUnarchivedChildAttachments.ts`, `createClaimsFromProcessedDocumentAttachments.ts`,
`archiveClaimedChildAttachments.ts`, `reconcileParentChargeSheets.ts`, and
more. A row written as `"paper charge sheet"` or `"PAPER_CHARGE_SHEET"`
would silently fall out of every one of those queries — it wouldn't error,
it would just be invisible to claim creation, archiving, and the reset/
reconciliation tooling. (This exact class of bug already happened once in
this table — a `'facesheet'` vs `'Facesheet'` mismatch, fixed after the fact
by migration `00134`. Getting the casing right at insert time avoids needing
the same kind of cleanup for your rows.)

## Rule 3 — populate `category` and `is_archived` on every insert (REQUIRED, not optional)

These two columns were added (migration `00133`) so Document Management's
dashboard can filter by real DB columns instead of re-deriving "is this a
claims doc, and is it archived" from `clm_att_path` on every request.
They're nullable with no default — `NULL` means "not set." At first, the
dashboard's query still fell back to computing both from the path for any
row where they were `NULL`, so leaving them unset was safe, just slower.
**That fallback was removed entirely as of 2026-09-04.** The dashboard's
list query (`combinedAttachmentRouter.ts`) is now a single SQL query with no
per-row JavaScript classification step at all — a row with `category IS
NULL` is now excluded from every single tab, unconditionally, with no
path-inference recovery. **A row inserted without these two columns set is
not "slower to find" anymore — it is invisible in Document Management,
permanently**, until someone notices and manually re-runs
`backfillAttachmentsCategoryAndArchiveStatus.ts` for it. Every in-repo writer
(Tebra, Practice Fusion, Document Management's own upload endpoints, the
Paper EOB redirect) already sets both on every insert as of this doc's date.

**`category`** — `varchar(20)`, one of:
- `"claims"` — for anything that ultimately feeds claim creation (this is
  the value the chargesheet flow always uses — it has no `"remittance"`
  case).
- `"remittance"` — for EOB/remittance documents.

**`is_archived`** — `boolean`:
- `false` for every row you insert (a freshly-created row is never archived
  yet).
- If your service is ever the one that moves a row's blob under an
  `Archive/...` prefix, flip this to `true` in the same update that changes
  `clm_att_path` to the archived location. Otherwise, don't set it to `true`
  — leave it `false` and let this repo's own archiving step (triggered once
  a claim is created from your row) do it.

Concretely, every row you insert should include something like:

```json
{ "category": "claims", "is_archived": false }
```

(`"remittance"` instead of `"claims"` if that's what your document is.)

## Status (`status`, varchar(5)) vocabulary

Free-form, but these are the values already in live use on this table —
reuse them rather than inventing new ones:

| Value | Meaning |
|---|---|
| `'T'` | Just uploaded, not yet processed. What a fresh row/parent is created with. |
| `'G'` | General / ready for review (also the column default). |
| `'E'` | Exception — extraction failed or needs manual attention. Set this on a row your service couldn't successfully extract, instead of silently omitting it or leaving it stuck at `'T'`. Existing reset/reconciliation tooling in this repo specifically looks for `status = 'E'` rows to find ones needing a re-run. |
| `'C'` | Completed / claimed — a claim has been created from this row (and, for the Facesheet flow, its blob archived alongside). Set by this repo's own claim-creation/archiving step, not by an external writer. |
| `'TR'` | Trashed. Not something an external writer should set. (Note: this used to be `'Z'` in older rows; `'Z'` is retired — don't reintroduce it, it collides with an unrelated, different meaning of `'Z'` on `claim_header.status`.) |

A reasonable default for a freshly-inserted, successfully-processed row is
`status: 'G'`; use `'E'` for one that failed extraction.

## Parent/child convention (chargesheet flow only)

Only applies if your integration actually splits one upload into multiple
documents the way the chargesheet flow does — most integrations don't need
this at all.

- The parent row has `parent_attachment_id IS NULL`.
- Every child gets `parent_attachment_id` = the parent's `id`,
  `parent_attachment_name` = the parent's `original_file_name`, and the
  **same** `attachment_type`, `client_id`, `group_id`, `practice_id` as the
  parent.
- Nothing in this repo validates a child's `attachment_type` against its
  parent at write time — but several scripts (`resetChargesheetsFromScratch.ts`,
  `reconcileParentChargeSheets.ts`) defensively check it and skip/flag any
  parent whose children don't all match. Keep them matching to avoid rows
  quietly falling out of that tooling.

## Other shared tables — `eob_deposits` / `eob_postings`

The external EOB extraction pipeline writes directly into
`"EDI_Tebra".eob_deposits` (one row per deposit/check) and `eob_postings`
(one row per claim/service-line posting within a deposit) after this repo's
own trigger call kicks it off — see `eobDepositProcessingRouter.ts` and
`DOCUMENT_MANAGEMENT_REMITTANCE_EOB_MERGE_PLAN.md` for the full context.
`eob_deposits.source_file` + `check_eft_number` together are unique
(`eob_deposits_source_file_check_eft_number_key`) and are what this repo's
own matching logic (`processPendingDeposits`) correlates back against.

**This integration is less finished than the chargesheet one — treat this
section as informational, not a completed contract.** In particular:
`billing_provider_npi` on `eob_deposits` is how this repo resolves
`client_id`/`group_id`/`practice_id` after the fact (NPI → `group.grpNpi` →
`group.clientId`) — so populate it whenever it's known, since a missing NPI
means a deposit's claims can't be scoped to a client at all. There is
currently no callback route in this repo for the pipeline to also write the
originating `attachments` row directly (the `eob_deposit_id`/`payer_name`/
`deposit_amount`/`deposit_date`/`billing_provider_npi` columns listed in the
column reference above) — that linkage is still done, when it's done at
all, entirely on this repo's side. If your team owns that pipeline and wants
to close this gap from your end, read the "BLOCKED" step in
`DOCUMENT_MANAGEMENT_REMITTANCE_EOB_MERGE_PLAN.md` first.

## Checklist for a new or existing integration

- [ ] `clm_att_path` (or your table's equivalent blob-path column) is a bare blob path on every insert/update — never a full `https://...` URL.
- [ ] `attachment_type` is one of the exact canonical literal values (Title Case) — see Rule 2.
- [ ] Don't set `document_dcn` — the DB trigger fills it in.
- [ ] Don't set `type_id`, `associated_claim_dcn`, or `claim_creation_response` — those are written later, by this repo's claim-creation step.
- [ ] Set `category` and `is_archived: false` on every row you insert — **required**, not optional: a row missing either is invisible in Document Management with no fallback (see Rule 3).
- [ ] Set `status: 'E'` on a row that failed extraction, instead of leaving it at a pre-processing status.
- [ ] Flip `processed: true` once `processed_extracted_data` is written for that row.
- [ ] Always supply `created_at` and `updated_at` explicitly (no DB default) and keep `updated_at` current on any later update to the same row.

## What changed and what you need to do (migration log)

Chronological. Each entry is a schema/behavior change that could affect an
external writer, with the action required to stay compatible. Entries with
no action item are context only.

- **`00093`** — added `upload_source`. No action required; optional, free-text.
- **`00109`/`00110`/`00111`** — `document_dcn` changed from a random UUID to a trigger-generated `YYYYMMDD` + sequence format. Action: stop generating your own `document_dcn` if you ever were — let the trigger fill it in.
- **`00130`/`00131`** — added the Facesheet-flow-only columns (`associated_patient_id`, `ocr_text`, `rpa_appointment_id`, `retry_count`, `sftp_file_path`, `json_manifest_entry`, `associated_patient_header_id`). No action required for any other flow — these are internal to this repo's own Tebra/Practice Fusion/PF Facesheet ingestion.
- **`00132`** — normalized the trashed status code from `'Z'` to `'TR'`. Action: if you ever wrote `'Z'` for a trashed row, stop — use `'TR'`. Realistically not applicable to any external writer, since none currently set a trashed status.
- **`00133`** — added `category` / `is_archived`. **Action, required as of `00137`'s follow-on (below): set both explicitly on every insert.** See Rule 3.
- **`00134`** — fixed an `attachment_type` casing bug (`'facesheet'` → `'Facesheet'`). Action: double-check your own `attachment_type` literal against Rule 2's canonical list — this exact bug is easy to reintroduce.
- **`00136`** — added a partial unique dedup index on `attachments` scoped to `upload_source = 'practice_fusion'`. No action for other writers, but a naming trap worth knowing: if a future integration reuses that same `upload_source` tag, its rows silently share that index's key space with Practice Fusion's.
- **`00137`** — added indexes on `group_id`/`practice_id`/`client_id`; also the point at which the dashboard's `category`/`is_archived` fallback (§ above, migration `00133`) was removed. No new columns, but this is the change that made Rule 3 mandatory instead of a nice-to-have — a row inserted without `category`/`is_archived` from this point on is invisible, not just slow to find.
- **`00138`** (2026-09-04) — added `eob_deposit_id`/`payer_name`/`deposit_amount`/`deposit_date`/`billing_provider_npi` to `attachments`, for the Paper EOB flow. No action for any current external writer — these are populated by this repo's own code, not by the EOB pipeline directly (see "Other shared tables" above).
- **`00139`** (2026-09-04) — added `processing_status`, internal to this repo's own async Facesheet upload handling. No action required for any external writer.
