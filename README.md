# Gradient Emission Tracker

Bot Python yang merekam emisi alpha/TAO untuk 22 hotkey tim Gradient di Bittensor subnet 56, dan menampilkannya di dashboard web sederhana (Dashboard per-hotkey + Captures wide-table history).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env             # isi TAOSTATS_API_KEY
cp config.example.yaml config.yaml  # edit team roster sesuai anggota kamu
```

## Run

### Local development (default, aman — hanya bind ke localhost)

```bash
uvicorn emission_tracker.main:create_app --factory --host 127.0.0.1 --port 8000
```

Buka <http://127.0.0.1:8000>.

### Server deployment (bind ke semua interface)

⚠️ Web UI **tidak punya authentication**. Kalau bind ke `0.0.0.0`, taro di belakang reverse proxy + auth (nginx Basic Auth / VPN / SSH tunnel).

```bash
# Hanya kalau yakin di-protect di lapisan lain:
uvicorn emission_tracker.main:create_app --factory --host 0.0.0.0 --port 8000
```

Atau lebih aman: tetap bind localhost lalu SSH-tunnel dari laptop:

```bash
# di VPS:
uvicorn emission_tracker.main:create_app --factory --host 127.0.0.1 --port 8000

# di laptop:
ssh -L 8000:127.0.0.1:8000 user@vps
# lalu buka http://127.0.0.1:8000 di browser lokal
```

### First-run note

Snapshot pertama dijalankan saat startup (jika `polling.run_on_startup: true`). Karena rate limit TaoStats (5 req/min) + 22 hotkey × 15s gap, snapshot pertama selesai sekitar **5-6 menit**. Refresh dashboard setelah itu.

## Test

```bash
pytest -v
```

89 tests, semua harus pass.

## Konfigurasi

### `.env` (tidak di-commit)
- `TAOSTATS_API_KEY` — required. Dapatkan dari <https://taostats.io/pro/>.
- `LOG_LEVEL` — optional, default INFO. Set `DEBUG` untuk verbose logging.

### `config.yaml` (NOT committed — copy from `config.example.yaml` and fill in team)
- `subnet_id`: 56 (Gradient).
- `polling.interval_minutes`: default 72 (~1 tempo Bittensor).
- `polling.request_interval_seconds`: default 15 (~4 req/menit, aman di bawah cap 5/menit).
- `polling.run_on_startup`: trigger snapshot saat boot.
- `database.path`: lokasi SQLite file (default `data/emissions.db`).
- `web.host`/`web.port`: catatan saja — uvicorn CLI argument override ini.
- `team`: list orang dengan hotkey-nya. Lihat file untuk format.

## Web pages

- **`/`** Dashboard — per-hotkey rows dengan Akumulasi, Status, Last refresh. Filter range (preset + custom date). Sortable columns.
- **`/captures`** Wide table — hotkey rows × N snapshot columns (default 20), scrollable horizontal, sticky kolom Hotkey & Name.

## JSON API

| Endpoint | Returns |
|---|---|
| `GET /api/healthz` | `"ok"` (liveness check) |
| `GET /api/persons?range=&from=&to=` | Per-person cumulative in date range |
| `GET /api/persons/{name}/series?range=&from=&to=` | Time-series for one person |
| `GET /api/hotkeys/{ss58}/series?range=&from=&to=` | Time-series for one hotkey |
| `GET /api/snapshots?limit=20` | List of recent snapshots (all statuses) |
| `GET /api/snapshots/latest` | Most recent snapshot |

## Arsitektur

Single-process: FastAPI + APScheduler. Snapshot worker tiap 72 menit fetch 22 hotkey (sequential, 5 req/min rate limit) lalu simpan ke SQLite. Web baca dari SQLite via FastAPI + Jinja2 + (vanilla JS for sort/charts).

```
TaoStats API → Snapshot Worker → SQLite ← FastAPI (web) ← Browser
                  ↑
            APScheduler tiap 72 min
```

Pada startup, semua snapshot dengan status `in_progress` (dari worker yang mati mid-flight — e.g. laptop sleep, crash, redeploy) di-mark `failed` supaya histori bersih.

Spec lengkap: [docs/superpowers/specs/2026-05-17-emission-tracker-design.md](docs/superpowers/specs/2026-05-17-emission-tracker-design.md).
Implementation plan: [docs/superpowers/plans/2026-05-17-emission-tracker.md](docs/superpowers/plans/2026-05-17-emission-tracker.md).

## TaoStats API contract (sudah diverifikasi live)

Endpoint, response shape, dan unit semua sudah diverifikasi 2026-05-17 di [src/emission_tracker/taostats_client.py](src/emission_tracker/taostats_client.py):

- Base URL: `https://api.taostats.io`
- Path: `GET /api/neuron/latest/v1?netuid=56&hotkey=<ss58>`
- Auth: `Authorization: <api-key>` header (no `Bearer` prefix)
- Response: `{"pagination": {...}, "data": [{"uid": int, "emission": str, "block_number": int, ...}]}`
- Empty `data: []` → treat as deregistered/not-found
- Emission is in **RAO** (1 alpha = 10⁹ RAO); UI displays alpha for readability

Verify yourself anytime:

```bash
curl -s "https://api.taostats.io/api/neuron/latest/v1?netuid=56&hotkey=<your-hotkey>" \
  -H "Authorization: $TAOSTATS_API_KEY" | head -c 2000
```

## Status

- **v0.1.0** + 17 polish commits — production-ready for internal team use.
- Future: notifikasi Telegram saat deregister, multi-subnet support, web auth.
