# Hướng dẫn test CDMS từng bước

Cập nhật: 2026-09-29. Hướng dẫn thực hành để **hiểu các giải pháp** của bài test qua việc tự chạy: mỗi bước gồm
*làm gì → kết quả mong đợi → chứng minh giải pháp nào*. Lệnh, cổng, test plan đầy đủ: [`testing.md`](docs/testing.md);
thiết kế: [`exactly-once.md`](docs/exactly-once.md), [`solution_VI.md`](docs/solution_VI.md); API: [`api.md`](docs/api.md).

Mọi lệnh chạy ở thư mục gốc repo, conda env `fastapi-env` (hoặc một venv Python 3.12 bất kỳ).

## Ý tưởng cốt lõi (đọc trước)

Mọi cơ chế (polling, webhook; Excel chỉ có thiết kế) đi qua **một pipeline duy nhất**:
normalize → fingerprint → detect → persist, **trong một transaction**. Chống trùng nằm ở database, 3 lớp:

| Lớp | Chặn | Cơ chế |
|---|---|---|
| Transport | cùng webhook gửi lại | `UNIQUE (source, event_id)` trên `inbox_event` |
| State | cùng trạng thái đến từ bất kỳ cơ chế nào | so fingerprint dưới row lock ⇒ `UNCHANGED` |
| Change log | hai writer tranh nhau một product | `SELECT … FOR UPDATE` + `UNIQUE (key, version)` |

Giao nhận là *at-least-once* (có retry), lưu trữ là *idempotent* ⇒ hiệu quả **exactly-once**.

Kết quả của pipeline cho mỗi product:

| Outcome | Khi nào | Ghi change? |
|---|---|---|
| `CREATED` | product chưa có | có (v1) |
| `UPDATED` | fingerprint khác, thời điểm quan sát không cũ hơn | có (v+1, kèm `diff`) |
| `UNCHANGED` | fingerprint giống | không |
| `STALE` | trạng thái cũ hơn trạng thái đang lưu | không |

## Các service và cổng

| Service | URL | Ghi chú |
|---|---|---|
| CDMS API + **UI** | `http://127.0.0.1:8100/ui` (`/docs` = Swagger) | `/` tự chuyển sang `/ui` |
| Emulator Vietful | `http://127.0.0.1:8101/docs` | `/` tự chuyển sang `/docs`; **chỉ có API, không có giao diện**; UI ở 8100 gọi API admin của nó. Gọi `/api/v1/...` trong `/docs`: bấm **Authorize**, dán `INVENTORY_API_TOKEN` (không kèm `Bearer `) |
| Worker | — | chạy poll theo lịch + xử lý job (webhook, "Poll now") |

## Bước 0 — Chuẩn bị

```bash
conda activate fastapi-env
pip install -r requirements-dev.txt -e .
alembic upgrade head
```

`.env` cần có `DB_*`, `INVENTORY_API_TOKEN`, `WEBHOOK_SECRET` (mẫu: `.env.example`).

## Bước 1 — Chạy test tự động (nhìn toàn cảnh nhanh nhất)

```bash
pytest tests/unit
pytest tests/integration      # dùng DB cdms_test (bị xóa sạch mỗi lần chạy)
```

File nên đọc để hiểu giải pháp:

- unit `canonical` / `fingerprint`: quy tắc normalize (trim, `""` → null, thứ tự `units` / `categories` không ảnh hưởng).
- integration pipeline: 50 task đồng thời ghi cùng một trạng thái ⇒ **1 change**; ghi các trạng thái khác nhau ⇒
  version liên tục, không deadlock.
- `tests/integration/test_cross_mechanism.py`: polling × webhook theo mọi thứ tự.

## Bước 2 — Khởi động 3 tiến trình

Ba terminal riêng (Windows cần `--loop cdms.loop:selector_loop`):

```bash
uvicorn cdms.emulator.main:app --port 8101 --loop cdms.loop:selector_loop
uvicorn cdms.api.main:app --port 8100 --loop cdms.loop:selector_loop
python -m cdms.worker
```

Kiểm tra: `http://127.0.0.1:8100/ready` và `http://127.0.0.1:8101/health` trả 200. Mở `http://127.0.0.1:8100/ui`.

Giao diện có 2 cột:

- **Vietful emulator**: Seed, Mutate (+ ô "send webhook"), Subscribe / Unsubscribe, Callback (duplicate rate,
  retries, delay, concurrency), Faults (mode, latency, error rate).
- **CDMS**: Poll now, cấu hình polling (bật / tắt theo lịch, interval, page size, timeout, số lần thử), bảng thống kê,
  product, change, webhook event, poll run; bấm một product để xem lịch sử version.

## Bước 3 — Seed dữ liệu và poll lần đầu ⇒ `CREATED`

1. Cột **Vietful emulator**: **Products** = `5000`, **Seed** = `42` → bấm **Seed (replace)**.
   UI báo `emulator seeded with 5000 products (seed 42)`.
   (Hoặc `POST http://127.0.0.1:8101/_admin/seed` với body `{"count":5000,"seed":42}` qua `/docs`.)
2. Bước trên **chỉ tạo dữ liệu trong emulator** (phía "Vietful"); CDMS chưa có gì.
3. Cột **CDMS**: bấm **Poll now**. Nút này chỉ đưa job vào hàng đợi — **worker** mới chạy poll.
4. Mong đợi (sau vài giây, auto-refresh 3 s): `products = 5000`, 5000 change `CREATED`, một poll run `SUCCEEDED`.

Lỗi khi bấm Seed ⇒ gần như chắc chắn emulator 8101 chưa chạy (trình duyệt gọi thẳng `:8101/_admin/seed`).

### Seed là gì?

**Seed** là số khởi tạo cho bộ sinh ngẫu nhiên (`random.Random(seed)` và `Faker.seed_instance(seed)`,
`src/cdms/emulator/data.py`). Máy tính sinh số "giả ngẫu nhiên" bằng công thức xuất phát từ seed:

- **cùng seed ⇒ cùng dãy số ⇒ cùng bộ dữ liệu** (từng field của từng product), mọi lần chạy, mọi máy;
- **khác seed ⇒ mã `P-00001`… giữ nguyên** (luôn đánh số từ 1), nhưng tên, màu, size, mô tả… khác.

Nhờ vậy test **tái lập được** và dùng được cho hai kiểm tra ở bước 4. **Seed (replace)** thay toàn bộ catalogue của
emulator; dữ liệu trong CDMS giữ nguyên.

## Bước 4 — Không có thay đổi ⇒ không ghi gì

1. Bấm **Poll now** lần nữa ⇒ số change **không tăng** (mọi product `UNCHANGED`).
2. Seed lại với **cùng seed 42** → **Poll now** ⇒ vẫn **0 change mới** (dữ liệu y hệt).
3. (Tùy chọn) Seed lại với **seed khác**, ví dụ `7` → **Poll now** ⇒ change `UPDATED` (v2) có `diff` cho các field
   khác nhau. Seed lại `42` rồi poll để quay về dữ liệu ban đầu (lại là v3).

⇒ Chứng minh: **chỉ lưu dữ liệu mới / thay đổi thật** — so sánh bằng fingerprint của trạng thái đã normalize.

## Bước 5 — Thay đổi không có webhook ⇒ polling phát hiện

1. **Mutate**: Count = `10`, **bỏ tick** "send webhook" → bấm **Mutate** (mỗi product đổi 1–2 field, giá trị thật
   sự khác).
2. **Poll now** ⇒ đúng **10 change `UPDATED`**.
3. Bấm một product vừa đổi (hoặc `GET /api/v1/products/{partnerSKU}/changes`) ⇒ v1 → v2, `diff {field: [cũ, mới]}`,
   `prevFingerprint` → `fingerprint`.

⇒ Chứng minh: **scheduled polling** — quét theo trang, mỗi trang một transaction, advisory lock để chỉ một run chạy.

## Bước 6 — Webhook, kể cả gửi trùng

1. Bấm **Subscribe this CDMS** (emulator sẽ gửi callback về `:8100/api/v1/webhooks/vietful`).
2. Callback: **Duplicate rate** = `0.3`, **Max retries** = `5`, **Retry delay ms** = `1000`, **Concurrency** = `4`
   → **Apply** (30 % callback bị gửi lặp, 4 luồng song song).
3. **Mutate**: Count = `20`, **tick** "send webhook" → **Mutate**.
4. Mong đợi:
   - `GET http://127.0.0.1:8101/_admin/callbacks`: số lần deliver **> 20** (có trùng);
   - bảng webhook event của CDMS: các lần lặp được trả `200 duplicate`, chỉ 20 event;
   - CDMS có **đúng 20 change mới**.

⇒ Chứng minh: lớp **Transport**. Webhook được lưu vào `inbox_event` + tạo job **trong cùng transaction** rồi trả `202`
ngay; worker xử lý theo lô qua pipeline. Trả lời nhanh, không mất event khi xử lý chậm.

## Bước 7 — Tự gửi webhook để thấy từng nhánh

Lưu đoạn sau vào một file ngoài repo (ví dụ `send_wh.py`) và chạy ở thư mục gốc repo (đọc `WEBHOOK_SECRET` từ
`.env`, ký HMAC-SHA256 như Vietful):

```python
import base64, hashlib, hmac, json, time, urllib.error, urllib.request, uuid

secret = next(l.split("=", 1)[1].strip() for l in open(".env") if l.startswith("WEBHOOK_SECRET="))


def send(eid, name, ts, sig_ok=True):
    body = json.dumps(
        {
            "id": eid,
            "timestamp": ts,
            "event": "PRODUCT_UPSERTED",
            "items": [
                {
                    "productId": 900001,
                    "sku": "SKU-DEMO",
                    "partnerSKU": "DEMO-1",
                    "productName": name,
                    "assetType": "GOODS",
                    "hasSerial": False,
                    "hasExpiration": False,
                    "isActive": True,
                    "units": ["PCS"],
                    "categories": [],
                }
            ],
        }
    ).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    req = urllib.request.Request(
        "http://localhost:8100/api/v1/webhooks/vietful",
        body,
        {"Content-Type": "application/json", "x-vf-hmacsha256": sig if sig_ok else "bad"},
    )
    try:
        r = urllib.request.urlopen(req)
        print(r.status, r.read().decode())
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode())


now = int(time.time())
a = str(uuid.uuid4())
send(a, "Demo v1", now)  # 202 accepted  → CREATED v1
send(a, "Demo v1", now)  # 200 duplicate (cùng id)
send(a, "Demo CHANGED", now)  # 409 EVENT_ID_CONFLICT (cùng id, nội dung khác)
send(str(uuid.uuid4()), "Demo v1", now)  # 202, nhưng UNCHANGED (cùng trạng thái, id mới)
send(str(uuid.uuid4()), "Demo v2", now + 10)  # 202 → UPDATED v2
send(str(uuid.uuid4()), "Demo v1", now - 10)  # 202, nhưng STALE (trạng thái cũ không ghi đè)
send(str(uuid.uuid4()), "x", now, sig_ok=False)  # 401 INVALID_SIGNATURE
```

Mong đợi: `GET /api/v1/products/DEMO-1/changes` **chỉ có v1 và v2**; bảng webhook event hiện outcome của từng event.

⇒ Chứng minh: đủ 3 lớp idempotency + **thứ tự** (dùng `timestamp` nguồn, trạng thái cũ không làm lùi trạng thái mới).

## Bước 8 — Chéo cơ chế: polling và webhook mang cùng một thay đổi

1. **Mutate** (tick "send webhook") → bấm **Poll now** ngay.
2. Mong đợi: mỗi thay đổi chỉ **1 change**; cơ chế đến sau ⇒ `UNCHANGED`. Trường `source` của change cho biết cơ chế
   nào đến trước.
3. Các thứ tự khó làm bằng tay (webhook đến trễ sau khi poll đã thấy trạng thái mới hơn, poll và webhook xử lý đồng
   thời, trạng thái cũ gửi lại với id mới) có trong `tests/integration/test_cross_mechanism.py`.

⇒ Chứng minh: exactly-once **trên trạng thái**, không phụ thuộc cơ chế nào mang nó tới.

## Bước 9 — Lỗi hệ thống

### Thủ công: Vietful bị sập

1. Faults: **Mode** = `down` → **Apply**. Mutate 10 product (bỏ tick webhook) → **Poll now**.
2. Mong đợi: poll run `FAILED` sau 3 lần thử có backoff; thay đổi chưa vào CDMS.
3. Faults: **Mode** = `ok` → **Apply** → **Poll now** ⇒ run `SUCCEEDED`, CDMS bắt kịp.

Thử thêm `slow` (latency) và `flaky` (error rate) để thấy timeout / retry.

### Tự động: F1–F5

Tắt worker ở bước 2 trước (script tự khởi động API 8110, emulator 8111 và worker riêng trên DB `cdms`, rồi dọn sạch):

```bash
python scripts/failure_scenarios.py          # hoặc: python scripts/failure_scenarios.py F2 F4
```

| ID | Tình huống | Điểm cần thấy |
|---|---|---|
| F1 | Worker chết giữa batch (trước commit) | 0 change bị commit dở; hết lease thì job được worker khác nhận lại; đúng 30 change |
| F2 | API chết sau khi commit inbox, trước khi trả lời | bên gửi retry ⇒ `200 duplicate`; 1 inbox row, 1 change |
| F3 | Vietful sập khi đang poll | run `FAILED`, sau khi phục hồi `SUCCEEDED` và bắt kịp |
| F4 | Mất kết nối PostgreSQL (cắt qua TCP proxy) | `/ready` 503, webhook trả 503 và bên gửi giữ lại; phục hồi ⇒ đủ 20 change |
| F5 | CDMS khởi động lại giữa lúc poll | run dở ⇒ `ABORTED`; mọi webhook được giao; 5 000 product khớp emulator |

Log ở `load/results/failure-logs/`.

⇒ Chứng minh: không có gì bị giữ trong bộ nhớ; mọi trạng thái trung gian (inbox, job, poll run) nằm trong DB và được
xử lý lại một cách idempotent.

## Bước 10 — Spike load + kiểm tra bất biến

Cần [k6](https://k6.io/) và 3 service đang chạy, emulator đã seed 5 000 và CDMS đã poll một lần.

```bash
python scripts/run_spike.py --peak 500
python scripts/verify_invariants.py --wait --prefix LT<run>-
```

Tải: 10 req/s → **500 req/s trong 60 s** → 10 req/s; 60 % trạng thái mới, 25 % gửi lại cùng `id`, 15 % trạng thái
cũ với `id` mới. Kết quả và phân tích: [`load-test-results.md`](docs/load-test-results.md) — bất biến luôn đúng; mục tiêu
latency / throughput D10 **chưa đạt** trên máy local (lý do và phương án trong tài liệu đó).

Kiểm tra bất biến bất kỳ lúc nào:

```bash
python scripts/verify_invariants.py
```

## Lưu ý và giới hạn

- **Excel upload chưa được implement** — chỉ có thiết kế ([`excel-solution.md`](docs/excel-solution.md), D13).
- Product đổi A → B → A trong **một** chu kỳ poll mà không có webhook ⇒ không thấy thay đổi (polling chỉ thấy ảnh
  chụp). Webhook lấp khoảng trống này.
- `timestamp` webhook chỉ chính xác đến giây: hai trạng thái khác nhau cùng một giây được áp dụng theo thứ tự đến.
- Xong thì tắt API, emulator, worker (Ctrl+C).
