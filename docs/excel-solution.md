# Excel upload — solution design (not implemented)

Status: **design only**. User decision 2026-09-29 (D13, revised): the Excel mechanism is documented, not built; the
implemented mechanisms are scheduled polling and the webhook callback. Everything below is how it would be built on
what exists (`cdms.core.pipeline`, the `job` queue of the webhook, the worker). Related: file format in
[`api.md`](api.md#excel-format-d6), decision D6 in [`assumptions.md`](assumptions.md).

## 1. Goal

A REST client uploads an `.xlsx` file of products. CDMS must store only new / changed products, exactly once, although:

- the **same file** may be uploaded again (user double-click, client retry);
- a file may contain the **same product twice**, or data **older** than what polling / webhook already stored;
- the upload may be large (up to 50,000 rows) and a crash may happen in the middle of processing;
- some rows may be invalid — they must not block the valid ones.

## 2. API

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/uploads/excel` (multipart `file`) | `202 {uploadId, status}` · `200` existing upload for the same file · `413 FILE_TOO_LARGE` · `415 UNSUPPORTED_FILE` · `422 UNKNOWN_COLUMN` |
| GET | `/api/v1/uploads/{id}` | status + counters (`totalRows`, `processedRows`, `created`, `updated`, `unchanged`, `stale`, `invalid`) |
| GET | `/api/v1/uploads/{id}/errors` | row errors (paged) |
| GET | `/api/v1/uploads/template.xlsx` | empty template with the header row |

File format (D6): sheet `Products` (else the first sheet), header in row 1, columns = `ProductDto` fields
(`partnerSKU` and `productName` required; `units` = `PCS,BOX`; `categories` = `C01:Phones;C02:Accessories`; optional
`productId`, `observedAt`). Unknown columns reject the file so typos are not silently ignored. Limits: 10 MB,
50,000 rows. The canonical normalizer already accepts these string forms (`cdms.core.canonical`).

## 3. Flow: accept durably, process asynchronously

```text
client ──POST file──► cdms-api                                     cdms-worker
                       1. size / type check, read header row
                       2. SHA-256 of the file bytes
                       3. BEGIN
                          INSERT excel_upload (file_sha256 UNIQUE, content, …)
                            ON CONFLICT (file_sha256) DO NOTHING RETURNING id
                          conflict → 200 with the existing upload
                          INSERT job (kind='excel_upload', ref_id=upload id)
                          COMMIT
                       ◄── 202 {uploadId}                          4. claim job (same queue as webhooks)
                                                                   5. parse with openpyxl (read-only), skip to
                                                                      processed_rows, then per chunk of 500 rows:
                                                                      BEGIN
                                                                        rows → ProductObservation
                                                                          (observed_at = observedAt column or the
                                                                           upload's received_at, source=EXCEL,
                                                                           source_ref=upload id + row number)
                                                                        apply_observations(chunk)
                                                                        upsert row errors
                                                                        processed_rows += chunk, counters
                                                                      COMMIT
                                                                   6. last chunk → status SUCCEEDED, job DONE
```

## 4. Why nothing is duplicated or lost

| Situation | Result |
|---|---|
| Same file uploaded again | `UNIQUE (file_sha256)` → the existing upload is returned, nothing reprocessed |
| Same product twice in a file | processed in row order inside `apply_observations` (duplicate keys take the row-by-row path) → the identical later row is `UNCHANGED` |
| File older than data from polling / webhook | `observedAt` older than stored → `STALE`; without `observedAt` the upload time is used (a manual upload is taken as "now") |
| Same data already stored by another mechanism | fingerprint equal → `UNCHANGED` |
| Invalid row (missing `partnerSKU`, bad boolean, …) | counted `invalid`, stored in `excel_row_error` with the raw row; the rest continues |
| Worker crashes in the middle | the chunk transaction rolls back; the job is re-claimed after its lease; parsing resumes at `processed_rows`; a re-run chunk is `UNCHANGED` and its row errors are upserted (`UNIQUE (upload_id, row_number)`) |
| API crashes after COMMIT, before answering | the client retries the same file → same SHA-256 → existing upload |
| Database down | API `503`, nothing stored |

## 5. Tables

- `excel_upload`: `id`, `file_name`, `file_sha256 bytea UNIQUE`, `content bytea`, `size_bytes`, `status`
  (`PENDING` / `PROCESSING` / `SUCCEEDED` / `FAILED`), `total_rows`, `processed_rows` (checkpoint), counters
  (`created`, `updated`, `unchanged`, `stale`, `invalid`), `error`, `received_at`, `finished_at`.
- `excel_row_error`: `upload_id FK`, `row_number`, `errors jsonb`, `raw jsonb`, `UNIQUE (upload_id, row_number)`.
- `job`: the same queue as webhooks, `kind = 'excel_upload'` ([`database.md`](database.md)).

Storing the file bytes in PostgreSQL (10 MB max) keeps the upload and its job in one transaction — no file system to
keep consistent with the database, and a crash can never leave a job without its file.

## 6. Performance notes

- Chunks of 500 rows: one commit per chunk (the local commit costs ~60 ms — ISSUES I-02), fast path for unchanged
  rows (I-01). New / changed rows take the row-by-row path (~5–15 ms each, I-03): a first import of 50,000 new
  products would take minutes; a set-based insert of new keys is the next optimization if needed.
- openpyxl read-only mode streams rows, so memory stays flat for large files.

## 7. Implementation outline (if built later)

| Piece | Where | Size |
|---|---|---|
| `openpyxl` dependency; `excel_upload`, `excel_row_error` models + migration | `cdms.db.models` | small |
| Upload route (limits, hash, header check, enqueue), status / errors / template routes | `cdms.api.routes.uploads` | medium |
| Row parser (header mapping, `observedAt`, raw row for errors) | `cdms.ingestion.excel` | medium |
| Job handler `excel_upload` (chunks, checkpoint, counters, row errors) | `cdms.ingestion.excel` | medium |
| Emulator `POST /_admin/export.xlsx` (demo / test files from the emulator catalogue) | `cdms.emulator` | small |
| Tests: same file twice, duplicate rows, stale rows, invalid rows, crash mid-file resume, 50,000 rows | `tests/` | medium |

Estimated effort: 1–1.5 days including tests.
