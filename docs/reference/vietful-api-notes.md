# Vietful API notes (source-backed)

Facts extracted on 2026-09-28 from Vietful's published OpenAPI document
(`https://ext.stg.vnfai.com/swagger/v1/swagger.json`, rendered at `https://ext.stg.vnfai.com/doc/index.html`).
A snapshot of the spec is kept in [`vietful-openapi.json`](vietful-openapi.json). Only the parts CDMS emulates are listed.
Anything not in this file is **not** known about Vietful and must not be assumed.

## Authentication

- OAuth2 client credentials (Keycloak realm), bearer `access_token`, refresh via `refresh_token`.
- The emulator does **not** implement OAuth2; it accepts a static bearer token (extension E1 in [`../api.md`](../api.md)).

## Products

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/Products` | Query `Keyword`, `PartnerSKUs` (comma list), `SKUs` (comma list), `PageIndex` (default 0), `PageSize` (default 10). "Ordered by product identity number." Response `200` = **JSON array** of `ProductDto` (no paging envelope). |
| GET | `/api/v1/Products/{partnerSKU}` | `ProductFullDetailDto` (superset of `ProductDto`). |
| GET | `/api/v1/Products/inventories` | Query `PartnerSKUs`, `SKUs`, `FromDate`, `ToDate` (`yyyy-MM-dd`), `PageIndex`, `PageSize`. Response is a paged envelope (`pageIndex`, `pageSize`, `totalCount`, `totalPages`, `indexFrom`, `items`, `hasPreviousPage`, `hasNextPage`). |
| PUT | `/api/v1/Products/{id}/swap-partner-sku`, `/update-partner-sku` | `partnerSKU` of an existing product **can change**. |

Errors: `400` with `{ "code": string, "errorMessage": string }`.

### `ProductDto` (list item)

| Field | Type |
|---|---|
| `productId` | int32 (Vietful internal id) |
| `sku` | string |
| `partnerSKU` | string |
| `productName` | string |
| `assetType` | string |
| `hasSerial` | boolean |
| `hasExpiration` | boolean |
| `color` | string |
| `size` | string |
| `description` | string |
| `isActive` | boolean |
| `units` | string[] |
| `categories` | `{ categoryCode: string, categoryName: string }[]` |

There is **no price and no updated-at / version field** on a product. (The requirement's `price 100 → 120` example is
illustrative only.)

### `InvProductBriefDto` (inventory item)

`warehouseCode`, `partnerSKU`, `sku`, `unitCode`, `conditionTypeCode`, `physicalQty`, `availableQty`, `pendingInQty`,
`pendingOutQty`, `freezeQty`, `inTransitQty` (int32), `lastUpdatedDate` (date-time), `categoryCode`, `categoryName`.

## Webhooks

- Subscribe: `POST /api/v1/WebhookSubscribers` with `{ "endpoint": string }`.
- Vietful **POSTs** to the endpoint; any non-2xx response is **retried after 15 minutes**.
- Authentication header `x-vf-hmacsha256` = Base64(HMAC-SHA256(raw body, `client_secret`)).
- Common body fields: `id` (request id), `timestamp` (Unix seconds, UTC), `event`, `errorCode`, `errorMessage`.
- Relevant event: **`INV_CHANGED`** — inventory of a product changed:

```json
{
  "event": "INV_CHANGED",
  "id": "ae2c1c84-e5e6-456f-9092-e61f70c7eab4",
  "timestamp": 1620113536,
  "warehouseCode": "TTNC",
  "changedReason": "PRODUCT_UNIT_CONVERTED",
  "items": [
    { "unitCode": "CAI", "sku": "string", "partnerSKU": "NB002", "conditionTypeCode": "NEW", "changeQty": 1190 }
  ]
}
```

`changeQty` is a **delta**, not a state. `changedReason` values include `PACKING_COMPLETED`, `RECEIVING_COMPLETED`,
`ADJUSTMENT_COMPLETED`, `FREEZE_COMPLETED`, `UNFREEZE_COMPLETED`, `QUICK_STORE`, … (full list in the spec).
Vietful documents **no product-master event** (no product created / updated webhook).

Other events (`IR_*`, `OR_*`) are out of scope.
