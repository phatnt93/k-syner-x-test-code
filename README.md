# CDMS — Change Data Management Service (kSynerX)

Prototype dịch vụ thu thập dữ liệu Product từ một **Vietful Inventory Service** giả lập qua **scheduled polling** và
**webhook callback** (Excel upload: chỉ thiết kế), và chỉ lưu **dữ liệu mới hoặc thay đổi thật — đúng một lần
(exactly once)** — kể cả khi có concurrency, retry, sự cố và tải đột biến (spike).

- Bài test: *CDMS — kSynerX FullStack take-home test* (tham chiếu §… trong README là mục của đề bài).
- Stack: Python 3.12, FastAPI, PostgreSQL 17 (SQLAlchemy 2 async + psycopg 3, Alembic), job queue trên PostgreSQL
  (không Redis), emulator dùng Faker, k6 cho load test, giao diện test tĩnh `/ui` (không build FE).

## Kiến trúc tóm tắt

```text
 Vietful emulator (:8101)                         CDMS
 ┌──────────────────────┐   GET /api/v1/Products   ┌──────────────────────────────────────────────┐
 │ Products API (Faker) │ <──────────────────────  │ cdms-worker: poll scheduler ─┐               │
 │ admin: seed / mutate │                          │                              v               │
 │        / faults      │  POST webhook (HMAC)     │ cdms-api (:8100) ─> inbox_event + job ─> job │
 │ callback outbox      │ ───────────────────────> │   202 / 200 duplicate          runner (batch)│
 └──────────────────────┘                          │                              │               │
                                                   │   pipeline: normalize → fingerprint → detect │
                                                   │   → persist (1 transaction) → products +     │
                                                   │     product_changes (append-only)            │
                                                   └──────────────────────────────────────────────┘
```

Mọi nguồn dữ liệu đều đi qua **một pipeline ghi duy nhất**: chuẩn hóa (canonical) → fingerprint SHA-256 → so sánh với
trạng thái hiện tại và timestamp nguồn → ghi trong **một transaction PostgreSQL** (insert-or-lock `products`, thêm dòng
vào `product_changes`). Chống trùng lặp nằm trong database chứ không nằm trong bộ nhớ ứng dụng:

| Loại trùng lặp | Cơ chế chặn |
|---|---|
| Webhook gửi lại (cùng event id) | `UNIQUE` trên `inbox_event` → trả `200 duplicate` |
| Cùng dữ liệu từ polling và webhook | fingerprint bằng nhau → `UNCHANGED`, không ghi change |
| Dữ liệu cũ đến sau dữ liệu mới | so `observed_at` → `STALE`, không ghi |
| Hai worker/request xử lý cùng product | `SELECT … FOR UPDATE` (khóa theo thứ tự `partner_sku`) + `UNIQUE (partner_sku, version)` |
| Worker chết giữa batch | job có lease; transaction chưa commit thì không có gì được ghi, job được claim lại |
| Hai lần poll chạy cùng lúc | advisory lock (single-flight) |

Chi tiết: [`docs/architecture.md`](docs/architecture.md), [`docs/exactly-once.md`](docs/exactly-once.md),
[`docs/database.md`](docs/database.md).

## Quick start

```bash
git clone git@github.com:phatnt93/k-syner-x-test-code.git
cd k-syner-x-test-code
```

Mọi lệnh dưới đây chạy ở thư mục gốc của repo.

### Cách 1 — Docker Compose (khuyến nghị để demo)

Cần Docker (Docker Desktop trên Windows):

```bash
python scripts/make_compose_env.py   # chỉ lần đầu: tạo compose.env với secret ngẫu nhiên (gitignored)
docker compose up -d --build         # postgres (volume riêng) → migrate → api, worker, emulator
```

- Mở `http://localhost:8100/ui` → **Seed (replace)** → **Subscribe this CDMS** → **Poll now** / **Mutate**.
- Swagger: `http://localhost:8100/docs` (CDMS), `http://localhost:8101/docs` (emulator).
- PostgreSQL của compose mở ở `127.0.0.1:5433` (cho script chạy từ host; user / password trong `compose.env`).
- Dừng: `docker compose down` (thêm `-v` để xóa luôn database).
- **Quy tắc cần nhớ:** mỗi lần tạo lại `compose.env`, phải xóa volume trước rồi mới `up` — PostgreSQL chỉ đọc
  `POSTGRES_PASSWORD` khi khởi tạo volume, nên `compose.env` mới cạnh volume cũ làm `migrate` lỗi
  `password authentication failed` (script sẽ cảnh báo nếu volume `cdms_pgdata` còn tồn tại). Thứ tự đúng:

  ```bash
  docker compose down -v                       # xóa luôn database của stack
  python scripts/make_compose_env.py --force
  docker compose up -d --build
  ```

Compose chạy `postgres` (image `postgres:17`) và 4 service dùng chung một image `cdms:local`: `migrate` (one-shot
`alembic upgrade head`), `api` (`uvicorn --workers 4`), `worker` (`python -m cdms.worker`), `emulator`.

### Cách 2 — Chạy từ source (development)

Cần Python 3.12 và một PostgreSQL có sẵn (hai database: `cdms` và `cdms_test` — `cdms_test` bị xóa mỗi lần chạy
integration test).

```bash
conda activate fastapi-env                # hoặc một venv Python 3.12 bất kỳ
pip install -r requirements-dev.txt -e .
cp .env.example .env                      # điền DB_*, INVENTORY_API_TOKEN, WEBHOOK_SECRET (chuỗi ngẫu nhiên)
alembic upgrade head
```

Chạy 3 process (mỗi process một terminal):

| Process | Lệnh |
|---|---|
| CDMS API + `/ui` | `uvicorn cdms.api.main:app --port 8100 --loop cdms.loop:selector_loop` |
| Vietful emulator | `uvicorn cdms.emulator.main:app --port 8101 --loop cdms.loop:selector_loop` |
| Worker (poll scheduler + job runner) | `python -m cdms.worker` |

`--loop cdms.loop:selector_loop` chỉ cần trên Windows (async psycopg cần selector event loop).

### Kiểm tra

```bash
pytest                                  # 206 test: unit + integration (trên cdms_test)
ruff check . && ruff format --check . && mypy
python scripts/failure_scenarios.py     # F1–F5, ~2 phút, tự khởi động và dọn process riêng
python scripts/verify_invariants.py     # kiểm tra invariant trên database cdms
```

Chi tiết lệnh, port, test plan: [`docs/testing.md`](docs/testing.md).

## Kịch bản demo (đề bài §15)

Tất cả thao tác dưới đây làm được trên `/ui` (bảng stats, thay đổi, product, inbox, poll run tự refresh 3 s; bấm
vào một product để xem lịch sử version và diff). Chuẩn bị: **Seed (replace)** 5.000 product → **Subscribe this CDMS**.
Hướng dẫn chi tiết từng bước (làm gì → kết quả mong đợi → chứng minh giải pháp nào):
[`test-guide_VI.md`](test-guide_VI.md).

| # | Kịch bản | Cách làm | Kết quả mong đợi |
|---|---|---|---|
| 1 | Create product | **Poll now** lần đầu | 5.000 change `CREATED` (version 1, source `POLLING`) |
| 2 | Detect change | Poll lại lần nữa khi không có gì đổi | poll run: `unchanged 5000`, **không** thêm change nào |
| 3 | Polling update | **Mutate** 20 product, bỏ chọn *send webhook* → **Poll now** (hoặc đợi lịch 30 s) | đúng 20 change `UPDATED` với diff từng field |
| 4 | Webhook update | **Mutate** 20 product, chọn *send webhook* | ~1 s sau có 20 change `UPDATED` source `WEBHOOK`; lần poll tiếp theo không ghi thêm (cross-mechanism duplicate) |
| 5 | Excel update | **Chưa implement** — chỉ có thiết kế: [`docs/excel-solution.md`](docs/excel-solution.md) | — |
| 6 | Duplicate prevention | **Callback delivery** → *Duplicate rate* 0.5 → Mutate có webhook | inbox có event trả `200 duplicate`; số change vẫn đúng bằng số product bị đổi |
| 7 | Concurrent processing | *Concurrency* 8 cho callback, Mutate 500 có webhook trong lúc poll đang chạy | mỗi product có version liên tục 1..n, không change trùng (`verify_invariants.py`) |
| 8 | Failure / retry | **Faults** `down` → Poll now → run `FAILED` sau 3 lần retry có backoff → Faults `ok` → Poll now | run sau `SUCCEEDED`, CDMS bắt kịp emulator. Kịch bản crash đầy đủ: `scripts/failure_scenarios.py` |
| 9 | Spike test | `python scripts/run_spike.py --peak 500` (hoặc k6 trong network compose) → `verify_invariants.py --wait` | xem [`docs/load-test-results.md`](docs/load-test-results.md) |

### Failure scenarios tự động (`scripts/failure_scenarios.py`) — lần chạy cuối: F1–F5 đều PASS

| ID | Sự cố | Kết quả đã kiểm chứng |
|---|---|---|
| F1 | Worker bị kill giữa batch (trước commit) | 0 change được commit; sau lease worker khác xử lý lại; đúng 30 change |
| F2 | API crash sau khi commit inbox, trước khi trả response | sender retry → `200 duplicate`; đúng 1 change |
| F3 | Inventory service down khi polling | run `FAILED` sau retry + backoff; sau khi phục hồi CDMS bắt kịp |
| F4 | Mất kết nối PostgreSQL (cắt qua TCP proxy) | `/ready` và webhook trả 503, sender giữ event; sau phục hồi đúng 20 change |
| F5 | Restart CDMS (API + worker) giữa lúc poll | run dở → `ABORTED`; sau restart toàn bộ 5.000 product khớp emulator |

### Spike test — kết quả chính

| Môi trường | Peak accept | Lỗi | p95 accept | Drain backlog | Tính đúng đắn |
|---|---|---|---|---|---|
| Dev (Windows host, PostgreSQL chung) | 139 events/s | 0.25 % | 19 s | ≤ 1 s | đạt mọi invariant |
| Compose, 4 API process, k6 trong network | **451 events/s** | **0 %** | 4.5 s | 101 s (mục tiêu ≤ 120 s) | đạt mọi invariant |

Mục tiêu D10 (500 req/s, p95 < 200 ms) **không đạt**; xem phần Hạn chế.

## Tính năng

Phạm vi (quyết định D13, [`docs/assumptions.md`](docs/assumptions.md)): polling và webhook được implement và demo;
Excel upload chỉ thiết kế.

### Implemented

- **Change pipeline**: chuẩn hóa, fingerprint, phát hiện `CREATED` / `UPDATED` / `UNCHANGED` / `STALE`, ghi exactly-once
  (row lock, constraint trong DB), lịch sử version append-only có diff; fast path theo batch cho trang không đổi.
- **Vietful Inventory Service emulator**: Products API theo OpenAPI của Vietful (Faker, bearer token, phân trang),
  admin seed / mutate / faults (`down`, `slow`, `flaky`), callback client gửi webhook có chữ ký HMAC từ outbox bền
  vững, có retry, gửi trùng theo tỉ lệ, gửi song song.
- **Scheduled polling**: scheduler trong worker, timeout + retry có backoff/jitter, single-flight bằng advisory lock,
  mỗi trang một transaction, tự đánh dấu `ABORTED` run bị gián đoạn; trigger thủ công qua API.
- **Webhook callback**: endpoint tương thích Vietful (HMAC), inbox bền vững phát hiện duplicate / conflict, job queue
  trên PostgreSQL (lease, retry có backoff, `DEAD`), xử lý theo batch trong worker.
- **Query / config API**: products, lịch sử version + diff, change feed, webhook inbox, poll runs, stats, cấu hình
  runtime (`GET/PUT /api/v1/config`); database lỗi → 503.
- **`/ui` test console**: stats trực tiếp, điều khiển emulator và CDMS, các bảng dữ liệu, lịch sử từng product.
- **Containerized deployment**: một image, `docker compose up` chạy PostgreSQL, migration, API, worker, emulator.
- **Failure scenarios F1–F5** tự động và **spike test** k6 + script kiểm tra invariant.
- **Cross-mechanism test (§13.3)**: cùng một trạng thái đến qua polling và webhook theo mọi thứ tự (poll →
  webhook, webhook → poll, poll bắt đầu trước webhook, webhook đến muộn sau trạng thái mới hơn, gửi lại với event id
  mới, poll và webhook xử lý đồng thời) → chỉ lưu một lần, trạng thái cũ không bao giờ ghi đè trạng thái mới
  (`tests/integration/test_cross_mechanism.py`).
- **Observability**: log có cấu trúc (`LOG_FORMAT=json`, compose bật sẵn) cho API, worker và emulator;
  correlation id: mỗi request có `X-Request-ID` (nhận từ caller hoặc tự sinh) gắn vào mọi dòng log của request, poll
  gắn `run_id`, worker gắn `worker_id` / `job_id`. Một webhook lần theo được bằng `event_id` / `inbox_id` từ dòng
  "webhook accepted" (API) đến "webhook event processed" kèm outcome (worker):
  `docker compose logs api worker | grep <event id>`.
- **Test**: 206 test (unit + integration, gồm concurrency 50 luồng cùng một product), ruff, mypy strict.

### Partially implemented

- **Spike load (đề bài §10)**: đã chạy và giữ đúng dữ liệu, drain backlog trong mục tiêu, nhưng **throughput / latency
  chưa đạt mục tiêu D10** (451 events/s, p95 4.5 s).

### Not implemented

- **Excel upload** — giải pháp mô tả trong [`docs/excel-solution.md`](docs/excel-solution.md), chưa code.
- **Inventory entity / sự kiện `INV_CHANGED`** — chỉ theo dõi Product master data.
- **Metrics** (Prometheus `/metrics`, dashboard) — chưa làm; hiện có `/api/v1/stats`, bảng `poll_run` / `inbox_event` và log có cấu trúc.

## Hạn chế

- Product được đối chiếu chỉ theo `partnerSKU` (D1): đổi tên partnerSKU trên Vietful tạo ra product "mới", hoán đổi
  partnerSKU được ghi thành thay đổi toàn bộ trên cả hai mã.
- A → B → A trong cùng một chu kỳ polling mà không có webhook thì polling không thấy.
- Thứ tự dựa trên timestamp nguồn (webhook chính xác đến giây — một thay đổi có timestamp webhook không mới hơn lần poll trước bị coi là `STALE` và được lần poll sau lưu, có test); timestamp webhook và poll đến từ **hai đồng hồ khác
  nhau** (I-13) — một lần poll bắt đầu sát sau webhook có thể bị coi là `STALE`, lần poll sau sẽ sửa. Không có dữ
  liệu sai, không có rollback.
- Product bị xóa khỏi inventory API không bị xóa trong CDMS (ngưng hoạt động thể hiện qua `isActive`).
- Throughput nhận webhook bị giới hạn bởi tốc độ commit của database (một commit mỗi event trước khi trả lời, để đảm
  bảo không mất event): ~60 req/s trên database dev, 451 events/s trong compose; p95 vẫn tính bằng giây. Khi accept
  nhanh, **một worker** (~250 events/s) trở thành nút cổ chai — có thể thêm worker replica (queue + row lock đã hỗ trợ).
- Trên Windows host, POST vào port Docker tốn thêm ~60 ms (I-15) — load test nên chạy k6 bên trong network compose.
- Emulator so khớp tên query parameter phân biệt hoa thường (I-09); poll thủ công trùng với lượt poll theo lịch sẽ bị
  bỏ qua (I-12).

Danh sách đầy đủ, số đo: [`docs/ISSUES.md`](docs/ISSUES.md).

## Bài học rút ra (Lessons learned)

1. **Commit latency quyết định throughput, không phải code.** Thiết kế "trả lời webhook chỉ sau khi event đã bền vững"
   nghĩa là mỗi request một commit. PostgreSQL dev (Docker trên ổ Windows) chỉ commit 26–284 lần/s, nên 500 req/s là
   bất khả thi dù code tối ưu đến đâu (I-02, I-14). Chuyển sang volume riêng trong compose nhanh gấp 7 lần — chỉ đổi
   deployment, không đổi code. Trade-off: **không** dùng `synchronous_commit = off` vì có thể mất event đã ack; hướng
   tiếp theo đúng là group commit trong API.
2. **Đo trước khi tối ưu.** Lần quét 5.000 product không đổi đầu tiên mất 30 s (bằng đúng chu kỳ poll). Profile cho
   thấy 3 round trip / product; fast path set-based (một `SELECT … FOR UPDATE` và một `UPDATE … FROM unnest` mỗi
   trang) giảm còn 3.5–4.9 s mà vẫn cho kết quả giống hệt đường row-by-row (có test so sánh) (I-01).
3. **Exactly-once phải nằm trong database.** Unique constraint (`inbox_event.id`, `(partner_sku, version)`), row lock
   theo thứ tự cố định và transaction bao trọn "phát hiện + ghi" là thứ duy nhất đứng vững trước crash giữa chừng
   (F1, F2) và 50 luồng đồng thời; bộ nhớ ứng dụng chỉ là tối ưu.
4. **Timeout phải có ở mọi tầng.** Khi PostgreSQL không truy cập được, psycopg trên Windows treo ~130 s, khiến sender
   webhook timeout thay vì nhận 503 để retry; thêm `connect_timeout` sửa được (I-11).
5. **Đồng hồ không đồng bộ là thật.** PostgreSQL trong Docker chạy nhanh hơn host ~50 ms, đủ để một lần poll bị đánh
   giá `STALE` (I-13). Thiết kế vẫn an toàn vì `STALE` không ghi gì — nhưng phải document thay vì giả định đồng hồ
   chung.
6. **Nền tảng ảnh hưởng đến kết quả đo.** Windows buộc async psycopg dùng `select()` loop (~512 socket, reset kết nối
   khi 1.000 client chờ); port proxy của Docker Desktop thêm ~60 ms / POST. Kết quả load test chỉ có ý nghĩa khi ghi
   rõ môi trường chạy.
7. **Dọn process sau test.** Dừng `timeout N python …` trên Windows để lại process python con chạy ngầm; một worker
   "lạc" đã xử lý job của một kịch bản failure khác. Luôn kiểm tra danh sách process sau khi dừng.

## Giải pháp cải thiện

Định hướng phát triển thêm.

1. **Load balancing cho API / webhook** — đặt load balancer (Nginx / HAProxy / Traefik) trước nhiều instance API;
   không cần sticky session vì API stateless và webhook gửi lại vào instance khác vẫn bị chặn trùng bởi
   `UNIQUE` của `inbox_event`; health check bằng `/ready`; thêm PgBouncer để tổng số kết nối không vượt
   `max_connections`.
2. **Tăng số worker khi job dồn** — chạy nhiều worker (`docker compose up -d --scale worker=N`); queue đã dùng
   `FOR UPDATE SKIP LOCKED` + lease nên các worker không tranh nhau; tự động scale theo `jobBacklog` /
   `oldestPendingJobAgeSeconds` của `/api/v1/stats`.
3. **Tách DB log và DB dữ liệu chính, DB cluster** — log ứng dụng đưa sang hệ thống log riêng (Loki / OpenSearch);
   dữ liệu mang tính log (`product_changes`, `inbox_event`, `job`, `poll_run` cũ) chuyển sang DB lưu trữ / phân tích
   riêng bằng CDC hoặc job archive, partition theo thời gian; PostgreSQL cluster 1 primary + N replica (Patroni):
   ghi và pipeline đọc ở primary, API tra cứu đọc ở replica; khi cần tăng ghi thì sharding theo `partner_sku`.
   Lưu ý: `products`, `product_changes`, `inbox_event`, `job` phải ghi trong cùng một transaction trên primary để
   giữ exactly-once — chỉ chuyển dữ liệu sang DB khác **sau khi** đã commit.
4. **Message broker (RabbitMQ) thay queue trên PostgreSQL khi tải rất lớn** — API ghi inbox rồi publish qua
   transactional outbox; worker consume, ghi DB rồi mới ack; dead-letter queue thay cho trạng thái `DEAD`. Đánh đổi
   về exactly-once: RabbitMQ chỉ đảm bảo at-least-once (message có thể giao lại, thứ tự không đảm bảo giữa nhiều
   consumer), nên chống trùng vẫn phải nằm ở DB (unique `inbox_event`, fingerprint, row lock, `observed_at`); thêm một
   hệ thống cần vận hành và giám sát.
5. **Metrics và cảnh báo (Prometheus + Grafana)** — endpoint `/metrics` cho API và worker; dashboard throughput,
   latency, backlog, tuổi job chờ lâu nhất; cảnh báo khi backlog vượt ngưỡng, có job `DEAD`, poll run `FAILED` liên
   tiếp, `/ready` trả 503.

## AI usage disclosure

Dự án được làm cùng AI coding agent **Claude (Claude Code)**.

**Ứng viên thực hiện:**

- Các quyết định thiết kế và phạm vi: đối chiếu product theo `partnerSKU` (D1), emulator chung codebase / schema
  riêng (D12), phạm vi webhook + polling, Excel chỉ thiết kế (D13), containerize bằng compose (I-05, option A), giữ
  thiết kế và document giới hạn spike (I-14, option C), cấu hình CORS / page size…
- Model `products` / `product_changes` đầu tiên và migration đầu tiên.
- Review mọi thay đổi, chạy và commit code; yêu cầu và duyệt từng task.

**AI (Claude) hỗ trợ / tạo ra, theo yêu cầu của ứng viên:**

- Phần lớn code ứng dụng: pipeline (canonical, fingerprint, apply), emulator + callback sender, polling, webhook + job
  queue, query / config API, `/ui` console, Dockerfile / compose.
- Test (unit, integration, concurrency), script failure scenarios, k6 spike + script kiểm tra invariant.
- Tài liệu trong `docs/` và README này; phân tích kết quả đo (I-01…I-15).

Mọi code do AI tạo đều được chạy qua test (206 test), ruff, mypy strict, failure scenarios F1–F5 và spike test; các
số liệu trong tài liệu là kết quả đo thật trên máy phát triển.

## Tài liệu

| | |
|---|---|
| Kiến trúc | [`docs/architecture.md`](docs/architecture.md) |
| Database | [`docs/database.md`](docs/database.md) |
| Exactly-once và xử lý sự cố | [`docs/exactly-once.md`](docs/exactly-once.md) |
| API (CDMS + emulator) | [`docs/api.md`](docs/api.md) |
| Giả định và quyết định thiết kế | [`docs/assumptions.md`](docs/assumptions.md) |
| Chạy, test, failure scenarios | [`docs/testing.md`](docs/testing.md) |
| Hướng dẫn test từng bước (thực hành) | [`test-guide_VI.md`](test-guide_VI.md) |
| Kết quả load test | [`docs/load-test-results.md`](docs/load-test-results.md) |
| Thiết kế webhook | [`docs/webhook-solution.md`](docs/webhook-solution.md) |
| Excel upload (chỉ thiết kế) | [`docs/excel-solution.md`](docs/excel-solution.md) |
| Tổng quan giải pháp | [`docs/solution_VI.md`](docs/solution_VI.md) ([EN](docs/solution.md)) |
| Vấn đề / số đo / quyết định | [`docs/ISSUES.md`](docs/ISSUES.md) |
| Ghi chú API Vietful (từ OpenAPI) | [`docs/reference/vietful-api-notes.md`](docs/reference/vietful-api-notes.md) |
