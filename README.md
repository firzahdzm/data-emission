# Gradient Emission Tracker

Bot Python yang merekam emisi alpha/TAO untuk 22 hotkey tim Gradient di Bittensor subnet 56, dan menampilkannya di dashboard web sederhana.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env             # isi TAOSTATS_API_KEY
# config.yaml sudah di-seed dengan team roster — edit kalau perlu ganti anggota
```

## Run

```bash
uvicorn emission_tracker.main:create_app --factory --host 0.0.0.0 --port 8000
```

Buka <http://127.0.0.1:8000>.

Snapshot pertama dijalankan saat startup (jika `polling.run_on_startup: true`). Karena rate limit TaoStats (5 req/min) + 22 hotkey × 15s gap, snapshot pertama selesai sekitar 5-6 menit. Refresh dashboard setelah itu.

## Test

```bash
pytest -v
```

58 tests, semua harus pass.

## Konfigurasi

### `.env` (tidak di-commit)
- `TAOSTATS_API_KEY` — required. Dapatkan dari <https://taostats.io/pro/>.
- `LOG_LEVEL` — optional, default INFO. Set `DEBUG` untuk verbose logging.

### `config.yaml` (committed)
- `subnet_id`: 56 (Gradient).
- `polling.interval_minutes`: default 72 (~1 tempo Bittensor).
- `polling.request_interval_seconds`: default 15 (~4 req/menit, aman di bawah cap 5/menit).
- `polling.run_on_startup`: trigger snapshot saat boot.
- `team`: list orang dengan hotkey-nya. Lihat file untuk format.

## Arsitektur

Single-process: FastAPI + APScheduler. Snapshot worker tiap 72 menit fetch 22 hotkey (sequential, 5 req/min rate limit) lalu simpan ke SQLite. Web baca dari SQLite via FastAPI + Jinja2 + Chart.js.

```
TaoStats API → Snapshot Worker → SQLite ← FastAPI (web) ← Browser
                  ↑                         
            APScheduler tiap 72 min
```

Spec lengkap: [docs/superpowers/specs/2026-05-17-emission-tracker-design.md](docs/superpowers/specs/2026-05-17-emission-tracker-design.md).
Implementation plan: [docs/superpowers/plans/2026-05-17-emission-tracker.md](docs/superpowers/plans/2026-05-17-emission-tracker.md).

## Untuk verifikasi TaoStats API contract

API endpoint paths dan field shapes dalam `taostats_client.py` adalah asumsi default. Sebelum production deploy, verifikasi dengan curl:

```bash
curl -s "https://api.taostats.io/api/dtao/neuron/v1?netuid=56&hotkey=<your-hotkey>" \
  -H "Authorization: $TAOSTATS_API_KEY" | head -c 2000
```

Jika response shape berbeda (misal field bukan `data[0].emission` tapi `data.emission` langsung), edit `src/emission_tracker/taostats_client.py::_parse_neuron`.

## Status

- v0.1.0: dashboard akumulasi per orang, chart historis, date filter, deregister detection.
- Future: notifikasi Telegram saat deregister, multi-subnet support, web auth.
