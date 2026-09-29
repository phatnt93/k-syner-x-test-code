# API

Status (2026-09-29): CDMS API implemented except the Excel routes (design only, D13); the runtime OpenAPI (`/docs`
on each service) is the reference — keep this file in sync with it.

Conventions (CDMS): JSON, UTC ISO-8601 timestamps, errors as
`{ "error": { "code": "STRING_CODE", "message": "…", "details": {…} } }`; database unreachable → `503 UNAVAILABLE` on
every endpoint. List endpoints use cursor paging `?limit=50&cursor=…` (limit 1–500) → `{ "items": [...], "nextCursor":
"…" | null }`; the cursor is opaque (the sort key of the last item), a bad one → `422 INVALID_CURSOR`. JSON is camelCase;
product fields keep Vietful's names (`partnerSKU`, `productId`). Fingerprints are hex. Every response carries
`X-Request-ID`: the caller's value when it is 1–64 characters of `[A-Za-z0-9._:-]`, otherwise a generated id; the same
id is on every log line of the request (C-14).

## CDMS (`cdms-api`, port 8100)

### Ingestion

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/webhooks/vietful` | `202 {status:"accepted", inboxId}` · `200 {status:"duplicate", inboxId}` · `401 INVALID_SIGNATURE` · `409 EVENT_ID_CONFLICT` · `413 PAYLOAD_TOO_LARGE` (> 1 MB) · `422 INVALID_PAYLOAD` · `503 UNAVAILABLE` (database down or `WEBHOOK_SECRET` unset). Implemented (C-05) |
| POST | `/api/v1/uploads/excel` (multipart `file`) (**not implemented**, design: [`excel-solution.md`](excel-solution.md)) | `202 {uploadId, status}` · `200` existing upload for the same file · `413 FILE_TOO_LARGE` · `415 UNSUPPORTED_FILE` |
| GET | `/api/v1/uploads/{id}` | status + counters (`totalRows`, `processedRows`, `created`, `updated`, `unchanged`, `stale`, `invalid`) |
| GET | `/api/v1/uploads/{id}/errors` | row errors (paged) |
| GET | `/api/v1/uploads/template.xlsx` | empty template with the header row |
| POST | `/api/v1/polling/run` | `202 {jobId}` — enqueue a manual poll (`poll_now` job); the worker runs it, or skips it if a run is in progress (the job's `last_error` says which). Implemented (C-05) |
| GET | `/api/v1/polling/runs` | poll run history, newest first, with counters (paged, default limit 20) |

#### Webhook request

Headers: `Content-Type: application/json`, `x-vf-hmacsha256: Base64(HMAC-SHA256(rawBody, WEBHOOK_SECRET))`.

```json
{
  "id": "5b6c…-uuid",
  "timestamp": 1790000000,
  "event": "PRODUCT_UPSERTED",
  "items": [
    { "productId": 1001, "sku": "SKU-1001", "partnerSKU": "P-1001", "productName": "Product A",
      "assetType": "GOODS", "hasSerial": false, "hasExpiration": false, "color": "red", "size": "M",
      "description": "…", "isActive": true, "units": ["PCS"],
      "categories": [ { "categoryCode": "C01", "categoryName": "Phones" } ] }
  ]
}
```

- `PRODUCT_UPSERTED` — extension **E2** (Vietful documents no product event). `items`: 1–500 full `ProductDto` states.
- `INV_CHANGED` — Vietful shape (see [`reference/vietful-api-notes.md`](reference/vietful-api-notes.md)); accepted and
  stored as `IGNORED` (processing it needs the inventory entity, C-12 — out of scope).
- Any other `event` → `202`, stored with status `IGNORED` (Vietful subscribers receive all events).

#### Excel format (D6)

Sheet `Products` (else first sheet), header in row 1:

| Column | Required | Format |
|---|---|---|
| `partnerSKU` | yes | text |
| `productName` | yes | text |
| `sku`, `assetType`, `color`, `size`, `description` | no | text |
| `hasSerial`, `hasExpiration`, `isActive` | no | `true/false/1/0/yes/no` |
| `units` | no | `PCS,BOX` |
| `categories` | no | `C01:Phones;C02:Accessories` |
| `productId` | no | integer |
| `observedAt` | no | ISO-8601; default = upload time |

Unknown columns → file rejected (`422 UNKNOWN_COLUMN`) so typos are not silently ignored.

### Query

| Method | Path | Notes |
|---|---|---|
Implemented 2026-09-29 (C-07): `cdms.api.routes.products`, `ops`, `webhooks`, `polling`; schemas in `cdms.schemas.api`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/products` | current states ordered by `partnerSKU`; filter `q` (substring of partnerSKU / sku / productName), `isActive` |
| GET | `/api/v1/products/{partnerSKU}` | current state + `version`, `fingerprint`, `observedAt`; unknown → `404 PRODUCT_NOT_FOUND` |
| GET | `/api/v1/products/{partnerSKU}/changes` | version history (v1, v2, …) with `diff` `{field: [old, new]}`, `prevFingerprint` → `fingerprint` |
| GET | `/api/v1/changes` | change feed by change id; `order=asc` (default, a consumer's feed) / `desc` (newest first); filter `source`, `since` (recorded at or after), `partnerSKU` |
| GET | `/api/v1/webhooks/events` | inbox rows, newest first (without payload, `items` = item count); filter `status` |
| GET | `/api/v1/webhooks/events/{id}` | one inbox row with its `payload`; unknown → `404 EVENT_NOT_FOUND` |
| GET | `/api/v1/stats` | `products`, `changesBySource`, `changesByType`, `inboxByStatus`, `jobsByStatus`, `jobBacklog`, `oldestPendingJobAgeSeconds`, `lastPollRun` — used by `/ui` and load tests |

### Config and ops

| Method | Path | Notes |
|---|---|---|
| GET / PUT | `/api/v1/config` | `sync_config` (D9). PUT changes only the fields sent; ranges = the table's CHECKs (interval 5–86400 s, page size 1–500, …) → `422 INVALID_PAYLOAD` otherwise. The worker applies changes on its next tick |
| GET | `/health` | liveness (no DB) |
| GET | `/ready` | DB reachable (`503` otherwise) |
| GET | `/ui` | HTML test console (C-08, `cdms/ui/static/index.html`, vanilla JS). Calls the CDMS API (same origin) and the emulator admin API from the browser (CORS). The emulator URL is injected from `UI_EMULATOR_URL` (default `INVENTORY_BASE_URL`). `GET /` redirects here |

## Inventory emulator (`cdms.emulator`, port 8101)

Same source / database as CDMS, separate process, schema `vietful` (D12). Implemented 2026-09-29 (C-03a) unless
marked C-03b / C-12.

### Vietful-compatible (mirrors the OpenAPI — do not change shapes)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/Products` | `Keyword` (case-insensitive substring of `sku` or `productName`), `PartnerSKUs`, `SKUs` (comma lists), `PageIndex` (default 0), `PageSize` (default 10); response = JSON array of `ProductDto`, ordered by `productId` |
| GET | `/api/v1/Products/{partnerSKU}` | `ProductDto` fields (subset of `ProductFullDetailDto`); unknown → `400 PRODUCT_NOT_FOUND` |
| POST | `/api/v1/WebhookSubscribers` | `{ endpoint }` → `204`; where callbacks are sent |
| GET | `/api/v1/Products/inventories` | paged envelope of `InvProductBriefDto` (C-12, not implemented) |

Errors use Vietful's `ExceptionErrorModel` body `{ "code", "errorMessage" }` (not the CDMS envelope). Vietful documents
only `400`; the emulator also returns `401 UNAUTHORIZED`, `500 INTERNAL_ERROR`, `503 SERVICE_UNAVAILABLE`, and invalid
query parameters as `400 INVALID_REQUEST` — error codes are the emulator's own.

CORS: the emulator allows browser calls only from `EMULATOR_CORS_ORIGINS` (default the CDMS console,
`http://localhost:8100` / `http://127.0.0.1:8100`).

Extensions: **E1** auth is `Authorization: Bearer <INVENTORY_API_TOKEN>` (static), not OAuth2; unset token →
`500 NOT_CONFIGURED`. It is declared as an `HTTPBearer` security scheme, so `/docs` shows an **Authorize** button
(paste the token without `Bearer `). `PageSize` is capped at 1000. Query parameter names are matched exactly (real Vietful, an
ASP.NET API, probably matches them case-insensitively — CDMS always sends the documented names).

### Admin (emulator-only, `/_admin`, no auth)

| Method | Path | Purpose |
|---|---|---|
| POST | `/_admin/seed` | `{ count, seed=42 }` — replace the catalogue with Faker products (deterministic per seed, ids from 1, `P-00001`…); clears the mutation log, then logs one `CREATED` per product |
| POST | `/_admin/products` | `{ count?, items?: [ProductDto-like, partnerSKU required], seed?, notify?: "none"\|"webhook" }` → `201 { mutations }`; `409 PRODUCT_EXISTS`, `409 NO_SUBSCRIBER` |
| POST | `/_admin/mutate` | `{ count, fields?: [ProductDto field names except partnerSKU], seed?, notify?: "none"\|"webhook" }` → `{ mutations }`; changes 1–2 fields of `count` random products, each value really different. `notify: "webhook"` queues one `PRODUCT_UPSERTED` callback per change in the same transaction (`409 NO_SUBSCRIBER` if no endpoint is registered) |
| GET | `/_admin/mutations` | `?cursor=&limit=100` → `{ items, nextCursor }`; the mutation log (ground truth for invariant 5) |
| GET / PUT | `/_admin/faults` | `{ mode: "ok"\|"down"\|"slow"\|"flaky", latencyMs, errorRate }` — applies to `/api/v1` only; `down` → 503 before auth |
| GET / PUT | `/_admin/subscriber` | `{ endpoint | null }` — same as Vietful's `WebhookSubscribers` but without the bearer token, for the `/ui` console; `null` unsubscribes |
| GET / PUT | `/_admin/callback` | `{ duplicateRate (0–1), maxRetries (5), retryDelayMs (2000), concurrency (4) }` — how webhooks are (re)delivered |
| GET | `/_admin/callbacks` | `?status=PENDING\|DELIVERED\|FAILED&cursor=&limit=100` → `{ items, nextCursor }`: the callback outbox (event id, attempts, deliveries incl. duplicates, last HTTP status / error) |
| POST | `/_admin/export.xlsx` | Excel export for the Excel mechanism — **not implemented** (Excel is design only, D13) |
| GET | `/health` | liveness |
| GET | `/` | redirects to `/docs` (Swagger); not in the OpenAPI schema |

Callbacks (C-03b, implemented 2026-09-29, `cdms.emulator.callbacks`): events go to the `vietful.callbacks` outbox in the
mutation's transaction; a sender loop in the emulator process POSTs them to the subscribed endpoint, signed with
`WEBHOOK_SECRET` (`x-vf-hmacsha256`), up to `concurrency` in parallel (so they may arrive out of order), retries every
non-2xx after `retryDelayMs` instead of Vietful's 15 minutes (extension E3) and gives up after `maxRetries`
(`FAILED`). Event `timestamp` = the mutation time in Unix seconds. With `duplicateRate` a delivered event is sent a
second time.
