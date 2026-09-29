# Tổng quan giải pháp (bản tiếng Việt)

Bản tiếng Việt của [`solution.md`](solution.md) — khi hai bản khác nhau, bản tiếng Anh là bản gốc. Chi tiết và lý do
thiết kế: [`architecture.md`](architecture.md), [`exactly-once.md`](exactly-once.md), [`database.md`](database.md),
các quyết định D-* trong [`assumptions.md`](assumptions.md). Viết ngày 2026-09-28; tên bảng theo model đã triển khai
(`products`, `product_changes`).

> Phạm vi (D13, 2026-09-29): polling và webhook được triển khai; các phần về **Excel** bên dưới (mục 7, 9) chỉ
> là thiết kế — xem [`excel-solution.md`](excel-solution.md).

## 1. Bài toán

Cùng một dữ liệu Product đi vào CDMS qua ba cơ chế, mỗi cơ chế có thể gửi **nhiều lần**, **sai thứ tự**, **cùng lúc**:

```text
Scheduled polling (CDMS định kỳ hỏi Vietful mỗi N giây) ─┐
Webhook callback  (Vietful chủ động đẩy event)          ─┼──► CDMS ──► PostgreSQL
Excel upload      (client upload file)                  ─┘
```

CDMS phải **chỉ lưu thay đổi thật, mỗi thay đổi đúng một lần**, kể cả khi:

- cùng dữ liệu đến hai lần (webhook retry, upload lại cùng file);
- cùng dữ liệu đến từ các cơ chế khác nhau (polling và webhook cùng thấy price 120);
- hai request cho cùng một product được xử lý đồng thời;
- process hoặc database crash giữa chừng.

**Ý tưởng cốt lõi:** không thể làm cho việc *gửi* là exactly-once (mạng retry, bên gửi gửi lại), nên CDMS làm cho việc
*lưu* là **idempotent** — xử lý lại cùng một input không tạo thêm tác dụng nào.
Gửi at-least-once + lưu idempotent = **exactly-once change**.

## 2. Hai bảng

| Bảng | Vai trò | Số dòng |
|---|---|---|
| `products` | **Trạng thái hiện tại** — phiên bản mới nhất đã biết của mỗi product | một dòng / `partner_sku` |
| `product_changes` | **Change Database** — lịch sử chỉ-thêm (append-only) của mọi thay đổi thật | một dòng / mỗi thay đổi |

Ví dụ: một product đổi giá hai lần.

```text
products:         P-1001 | price=150 | version=3 | fingerprint=C

product_changes:  P-1001 | v1 | CREATED | prev=∅ → A | data {price:100}
                  P-1001 | v2 | UPDATED | prev=A → B | diff {price:[100,120]}
                  P-1001 | v3 | UPDATED | prev=B → C | diff {price:[120,150]}
```

## 3. Fingerprint — quyết định "có thay đổi hay không"

Mọi product đi vào đều được **chuẩn hóa (normalize)** về một dạng canonical duy nhất, bất kể nguồn:

- chuỗi được trim, `""` → `null`;
- boolean được parse (`true` / `1` / `yes`);
- `units` và `categories` được sắp xếp, nên thứ tự khác nhau không bị coi là thay đổi;
- bỏ metadata vận chuyển (event id, timestamp, source).

Sau đó `fingerprint = SHA-256(canonical JSON)`. So sánh hai hash 32 byte thay cho việc so từng field.

| Tình huống | Kết quả |
|---|---|
| `partner_sku` chưa có trong `products` | **CREATED** — insert dòng state + change v1 |
| Fingerprint khác với bản đang lưu | **UPDATED** — version + 1, insert dòng change |
| Fingerprint giống | **UNCHANGED** — không ghi gì |
| Dữ liệu cũ hơn state đang lưu | **STALE** — bỏ qua (mục 8) |

Cơ chế này cũng loại trùng **giữa các cơ chế**: nếu polling đã lưu "price 120" trước, webhook mang cùng dữ liệu sẽ có
cùng fingerprint và thành UNCHANGED.

## 4. Một pipeline chung cho cả ba cơ chế

Ba cơ chế chỉ khác nhau ở cách **nhận** dữ liệu; sau đó tất cả đi qua cùng một hàm:

```text
Polling page  ─┐
Webhook event ─┼─► normalize ─► fingerprint ─► apply_observation() ─► DB
Excel row     ─┘
```

Chỉ một chỗ phải làm đúng, một chỗ để test, không có nguy cơ ba đoạn kiểm tra trùng hơi khác nhau.

## 5. `apply_observation` — transaction exactly-once

Với mỗi product, trong **một transaction database**:

```text
BEGIN
 1. INSERT INTO products (...) ON CONFLICT (partner_sku) DO NOTHING
      → insert được?  CREATED: insert product_changes v1, COMMIT
 2. SELECT ... FROM products WHERE partner_sku = ? FOR UPDATE
      → khóa dòng: các transaction khác cho CÙNG product phải chờ ở đây
 3. so sánh:
      observed_at của input cũ hơn bản đang lưu → STALE      → COMMIT (không ghi)
      cùng fingerprint                          → UNCHANGED  → COMMIT (không ghi)
      ngược lại:
        UPDATE products SET version = version + 1, fingerprint = mới, data..., observed_at = ...
        INSERT product_changes (version = cũ + 1, prev_fingerprint = cũ, fingerprint = mới, diff)
COMMIT
```

- **Atomic (nguyên tử):** cập nhật state và dòng change cùng commit hoặc cùng không — crash không để lại dữ liệu dở dang.
- **Tuần tự theo từng product:** `FOR UPDATE` bắt các writer đồng thời của cùng một product phải lần lượt; các product
  khác nhau không bao giờ chặn nhau.

## 6. Race condition (yêu cầu §8.2)

Polling và webhook cùng mang P-1001 với price 120 tại cùng một thời điểm.

Cách làm ngây thơ "kiểm tra rồi mới insert":

```text
A: kiểm tra → chưa đổi     B: kiểm tra → chưa đổi
A: insert change           B: insert change          → TRÙNG ❌
```

Thiết kế này:

```text
A: SELECT ... FOR UPDATE   (lấy được khóa)
B: SELECT ... FOR UPDATE   (phải chờ)
A: fingerprint khác → version 2, insert change → COMMIT (nhả khóa)
B: đọc được version 2, fingerprint trùng của mình → UNCHANGED   ✅ chỉ một change
```

**Lưới an toàn trong database:** `product_changes` có `UNIQUE (partner_sku, version)` và
`CHECK (prev_fingerprint IS DISTINCT FROM fingerprint)`. Kể cả code có bug, PostgreSQL vẫn từ chối v2 thứ hai hoặc một
"change" mà không thay đổi gì. **Database là nơi quyết định cuối cùng**, không phải bộ nhớ ứng dụng — API và worker là
các process riêng, không chia sẻ bộ nhớ.

## 7. Trùng ở tầng vận chuyển — idempotency key

Một số bản trùng bị chặn trước khi vào pipeline:

| Cơ chế | Idempotency key | Khi trùng |
|---|---|---|
| Webhook | `id` trong envelope → `UNIQUE (source, event_id)` trong `inbox_event` | `200 duplicate`, không xử lý lại |
| Excel | SHA-256 của file → `UNIQUE file_sha256` trong `excel_upload` | trả về upload đã có |
| Polling | không cần | dựa trên state: product không đổi ⇒ UNCHANGED |

Tầng này chỉ để tối ưu; tính đúng vẫn đến từ mục 5.

## 8. Thứ tự — dữ liệu cũ đến muộn (STALE)

Trường hợp mà fingerprint một mình không bắt được:

```text
10:00  webhook A: price 100 → đã lưu
10:01  webhook B: price 120 → đã lưu (v2)
10:02  webhook A được gửi lại (retry)
```

Nếu chỉ dùng fingerprint, "100 ≠ 120" sẽ bị ghi thành một change mới và đưa product về dữ liệu cũ — sai. Vì vậy mỗi
state lưu `observed_at` — thời điểm nguồn nhìn thấy state đó:

- webhook → `timestamp` của event;
- polling → thời điểm **bắt đầu** request trang (không bao giờ nhận là mới hơn thực tế);
- Excel → cột `observedAt`, nếu không có thì là thời điểm upload.

Input có `observed_at` cũ hơn bản đang lưu là **STALE** và bị bỏ qua — chính là yêu cầu "không lưu dữ liệu cũ".
Input UNCHANGED nhưng có `observed_at` mới hơn vẫn đẩy `observed_at` đang lưu lên (không ghi change), nên một lần
poll lúc 10:05 xác nhận giá 120 sẽ làm một webhook đến trễ có `observed_at` 10:03 cũng thành STALE.

## 9. Webhook và Excel — nhận trước, xử lý sau

```text
POST /webhooks/vietful ──► kiểm tra HMAC ──► INSERT inbox_event + job (1 transaction) ──► 202 Accepted
                                                          │
                                     worker ◄─────────────┘  nhận job (FOR UPDATE SKIP LOCKED),
                                                             chạy apply_observation, đánh dấu job DONE
                                                             trong CÙNG transaction
```

- **Spike load:** mỗi request chỉ làm một insert nhỏ rồi trả về nhanh; worker xử lý dần backlog theo tốc độ của nó.
- **An toàn khi crash:** khi API đã trả `202`, event đã nằm trên đĩa. Nếu worker chết, lease của job hết hạn và worker
  khác nhận lại; chạy lại không gây hại (mục 5).
- **Crash sau khi commit nhưng trước khi trả response:** bên gửi retry → event id đã tồn tại → `200 duplicate` → không
  có change thứ hai.

Hàng đợi là một bảng `job` trong PostgreSQL (`SELECT ... FOR UPDATE SKIP LOCKED`) — không cần Redis / Kafka. Upload
Excel dùng chung hàng đợi này và được xử lý theo từng chunk, có checkpoint `processed_rows`.

## 10. Xử lý lỗi (yêu cầu §9)

| Lỗi | Hành vi | Vì sao không mất, không trùng |
|---|---|---|
| **CDMS crash giữa chừng** | transaction bị rollback; job chạy lại khi lease hết hạn | không có gì commit dở; retry là idempotent |
| **Vietful / emulator sập** | timeout, 3 lần retry có backoff, poll run `FAILED`; lượt sau thử lại | webhook đã nhận vẫn nằm trong inbox chờ xử lý |
| **PostgreSQL sập** | API trả `503`, bên gửi retry; worker chờ rồi kết nối lại | không giữ gì trong bộ nhớ ⇒ process chết không làm mất gì |
| **Không rõ commit xong chưa** (mất kết nối) | coi như thất bại và retry | lần retry sẽ là UNCHANGED / duplicate |
| **Máy chủ crash** | dữ liệu Postgres bền trên đĩa; khi khởi động lại, worker xử lý tiếp job đang chờ và hủy poll run treo | đã commit ⇒ bền; chưa commit ⇒ được retry |

## 11. Spike load test (yêu cầu §10)

Kịch bản k6: 10 req/s → **500 req/s trong 60 s** → 10 req/s; trộn dữ liệu mới, gửi lại y hệt, và trùng giữa các cơ
chế, đồng thời polling và upload Excel vẫn chạy. Đo:

- throughput, latency p95, tỉ lệ lỗi;
- thời gian để backlog job xử lý hết sau spike;
- quan trọng nhất — kiểm **SQL invariant** sau khi chạy (`scripts/verify_invariants.sql`): version của mỗi product
  liên tục 1..n, mỗi change nối đúng fingerprint trước đó, `products.version` bằng change mới nhất, không có bản trùng.

Chi tiết: [`testing.md`](testing.md).

## 12. Giới hạn đã biết

- Product chỉ được so khớp theo `partner_sku` (quyết định đã chốt, D1). Nếu Vietful đổi partnerSKU
  (`update-partner-sku`), CDMS thấy một product mới dưới mã mới và lịch sử bị tách làm hai; nếu hai product hoán đổi mã
  (`swap-partner-sku`), cả hai đều bị ghi nhận là thay đổi toàn bộ dữ liệu.
- Product đổi A → B → A **trong cùng một chu kỳ polling** mà không có webhook thì polling không thấy (polling chỉ nhìn
  thấy snapshot).
- Thứ tự dựa vào timestamp của nguồn (webhook chính xác tới giây); một lượt poll bắt đầu ngay trước một webhook mới hơn
  có thể bị coi là stale và sẽ được lượt poll kế tiếp sửa lại.
- Webhook `INV_CHANGED` thật của Vietful mang **delta** số lượng (`changeQty`), không phải state. CDMS không bao giờ cộng
  delta: event chỉ được coi là tín hiệu, CDMS lấy lại state hiện tại, nên delta bị gửi lại không thể bị cộng hai lần.

## 13. Yêu cầu → cơ chế

| Yêu cầu | Cơ chế |
|---|---|
| Chỉ lưu dữ liệu mới / thay đổi | so fingerprint → CREATED / UPDATED / UNCHANGED |
| Không lưu dữ liệu cũ | `observed_at` → STALE |
| Exactly-once update | một transaction + khóa dòng + `UNIQUE (partner_sku, version)` + idempotency key |
| Các cơ chế chạy đồng thời | `FOR UPDATE` theo từng product; database là nơi quyết định cuối cùng |
| Phục hồi sau lỗi | transaction nguyên tử, bảng inbox / job bền vững, retry idempotent |
| Spike load | nhận trước xử lý sau, hàng đợi PostgreSQL, kiểm tra invariant |

## 14. Cấu trúc `product_changes` (model tiếp theo)

| Cột | Mục đích |
|---|---|
| `id` | thứ tự toàn cục của các change |
| `partner_sku`, `version`, `change_type` | product nào, version mấy, `CREATED` / `UPDATED` |
| `prev_fingerprint` → `fingerprint` | bước chuyển trạng thái mà change này thể hiện |
| `data`, `diff` | toàn bộ state mới; `{field: [cũ, mới]}` |
| `source`, `source_ref` | `POLLING` / `WEBHOOK` / `EXCEL` và poll run, event id hoặc dòng file đã tạo ra nó |
| `observed_at`, `recorded_at` | thời điểm nguồn quan sát; thời điểm CDMS lưu |

Ràng buộc: `UNIQUE (partner_sku, version)`, `CHECK (prev_fingerprint IS DISTINCT FROM fingerprint)`,
`CHECK ((version = 1) = (prev_fingerprint IS NULL))`.
