# Gradient Subnet Emission Tracker — Design

**Date:** 2026-05-17
**Status:** Draft (pending user review)
**Project:** `data-emmision`

## 1. Overview

Bot Python + SQLite + FastAPI yang merekam emisi TAO/alpha untuk anggota tim Gradient subnet (Bittensor subnet 56), lalu menampilkannya di dashboard web sederhana dengan fokus pada akumulasi per orang dan deteksi deregistrasi hotkey.

### 1.1 Tujuan

- Memantau performa kontribusi 11 anggota tim (22 hotkey, 2 per orang) di subnet 56.
- Mencatat snapshot emisi tiap tempo (~72 menit) untuk akumulasi historis.
- Memberi visibilitas cepat saat hotkey "ketendang" dari subnet.

### 1.2 Out of scope (v1)

- Notifikasi push (Telegram/email) saat deregistrasi. Schema sudah siap; tinggal ditambah cron kecil di iterasi berikutnya.
- Multi-subnet tracking (hanya subnet 56 untuk sekarang).
- Authentication di web UI (asumsi: dijalankan internal/private).
- Komparasi dengan tim lain di subnet yang sama.

## 2. Architecture

Tiga komponen dalam satu proses Python (single-process design).

```
┌─────────────────────────────────────────────────┐
│                  Single Process                  │
│                                                  │
│   ┌────────────┐    write    ┌──────────────┐   │
│   │  Snapshot  │ ─────────► │   SQLite     │   │
│   │  Worker    │            │  (file.db)   │   │
│   │ (per tempo)│            └──────┬───────┘   │
│   └─────┬──────┘                   │ read       │
│         │ HTTP                     ▼             │
│         ▼                   ┌──────────────┐   │
│   ┌─────────────┐           │   FastAPI    │   │
│   │  TaoStats   │◄────read──┤   + Static   │◄──┼── Browser
│   │     API     │           │   Frontend   │   │
│   └─────────────┘           └──────────────┘   │
└─────────────────────────────────────────────────┘
```

- **Snapshot Worker:** APScheduler-driven; tiap tempo memanggil TaoStats API per hotkey lalu menulis ke SQLite.
- **SQLite:** satu file di `data/emissions.db`; single source of truth.
- **FastAPI:** serve JSON endpoints + halaman HTML (Jinja2 + Chart.js).
- **Boundary eksternal:** hanya TaoStats API.

Worker hanya menulis; web hanya membaca. Tidak ada IPC; mereka berkomunikasi via SQLite file. Pemisahan ini membuat keduanya dapat di-test independen dan mudah dipecah jadi 2 proses jika nanti dibutuhkan tanpa perubahan schema.

## 3. Data Model (SQLite)

### 3.1 Schema

```sql
-- Reference data (sync dari config saat startup, idempotent upsert)
CREATE TABLE persons (
  id     INTEGER PRIMARY KEY,
  name   TEXT UNIQUE NOT NULL
);

CREATE TABLE hotkeys (
  ss58       TEXT PRIMARY KEY,
  person_id  INTEGER NOT NULL REFERENCES persons(id),
  subnet_id  INTEGER NOT NULL
);

-- Time-series (ditulis bot tiap tempo)
CREATE TABLE snapshots (
  id            INTEGER PRIMARY KEY,
  taken_at      TIMESTAMP NOT NULL,
  block_number  INTEGER,
  status        TEXT NOT NULL                  -- 'in_progress' | 'ok' | 'partial' | 'failed'
);

CREATE TABLE neuron_snapshots (
  snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
  hotkey_ss58    TEXT    NOT NULL REFERENCES hotkeys(ss58),
  uid            INTEGER,                      -- NULL kalau deregistered
  emission       REAL,                         -- NULL kalau deregistered
  is_registered  BOOLEAN NOT NULL,
  PRIMARY KEY (snapshot_id, hotkey_ss58)
);

CREATE INDEX idx_neuron_snap_hotkey ON neuron_snapshots(hotkey_ss58);
CREATE INDEX idx_snapshots_taken_at ON snapshots(taken_at DESC);
```

### 3.2 Semantik field `emission`

- `emission` = nilai mentah per snapshot dari TaoStats API.
- **Akumulasi tidak disimpan sebagai kolom**; dihitung on-the-fly via `SUM(emission)` di query.
- Asumsi user: tiap nilai snapshot adalah "emisi yang didapat selama tempo terakhir" dan boleh di-SUM antar snapshot untuk dapat total akumulasi.
- **Catatan implementasi:** semantik field di API harus diverifikasi saat coding. Kalau API ternyata balikin *rate* (TAO/day) bukan delta per tempo, kita harus pilih field/endpoint berbeda atau lakukan konversi. Akan dikonfirmasi ke user kalau perlu ganti pendekatan.

### 3.3 Aturan deregistrasi

- Saat polling, kalau API balikin "neuron not found" untuk satu hotkey: tetap INSERT row dengan `is_registered=FALSE`, `uid=NULL`, `emission=NULL`.
- Tujuan: jejak historis tetap utuh; bisa hitung "last seen registered" via `MAX(taken_at) WHERE is_registered=TRUE`.

### 3.4 Lifecycle data referensi

- Saat startup: read `team` dari `config.yaml` → upsert ke `persons` & `hotkeys` (idempotent).
- Hotkey yang dihapus dari config **tidak di-delete** dari DB; histori preserved.
- Untuk benar-benar hapus, dilakukan manual via tool admin (tidak dalam scope v1).

## 4. Configuration

### 4.1 `.env` (gitignored)

```bash
TAOSTATS_API_KEY=tao-xxxxxxxxxxxxxxxx
```

### 4.2 `config.yaml` (committed; hotkey adalah data publik on-chain)

```yaml
subnet_id: 56

polling:
  interval_minutes: 72              # ~1 tempo Bittensor
  request_interval_seconds: 15      # jarak antar API call (≈4 req/menit, di bawah cap 5/menit)
  run_on_startup: true

database:
  path: data/emissions.db

web:
  host: 127.0.0.1
  port: 8000

team:
  - name: Bob
    hotkeys:
      - 5BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB1
      - 5BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2
  - name: Carol
    hotkeys:
      - 5CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC1
      - 5CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC2
  - name: Dave
    hotkeys:
      - 5DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD1
      - 5DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD2
  - name: Alice
    hotkeys:
      - 5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1
      - 5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2
  - name: Eve
    hotkeys:
      - 5EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE1
      - 5EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE2
  - name: Frank
    hotkeys:
      - 5FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF1
      - 5FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF2
  - name: Grace
    hotkeys:
      - 5GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG1
      - 5GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG2
  - name: Henry
    hotkeys:
      - 5HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH1
      - 5HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH2
  - name: Iris
    hotkeys:
      - 5JJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJ1
      - 5JJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJ2
  - name: Jack
    hotkeys:
      - 5KKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKK1
      - 5KKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKK2
  - name: Kim
    hotkeys:
      - 5LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL1
      - 5LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL2
```

### 4.3 Validasi (Pydantic-settings)

- `name` unik di seluruh team.
- `hotkeys` valid SS58 (regex: starts with `5`, length 47–48 char, base58 alphabet).
- Tidak ada hotkey duplikat antar orang.
- Fail-fast saat startup kalau invalid; exit non-zero dengan error jelas. Tidak ada silent fallback.

## 5. Snapshot Worker (Bot)

### 5.1 Scheduling

- APScheduler `BackgroundScheduler` di-start saat FastAPI `startup` event.
- 1 job: `take_snapshot()`, interval = `polling.interval_minutes`.
- `max_instances=1` → cegah overlap kalau run sebelumnya nyangkut > 1 interval.
- `coalesce=True` → kalau scheduler ketinggalan (proses idle/sleep), hanya 1 catch-up run.
- `run_on_startup` (manual fire saat `lifespan` open) → snapshot pertama langsung di-trigger biar dashboard tidak kosong.

### 5.2 Rate limiter

- Cap: 5 req/menit (limit TaoStats).
- Implementasi: token bucket sederhana, 5 token kapasitas, refill 1 token tiap 12 detik (= 5/menit rate).
- Default config: 15 detik antar request (~4 req/menit). Buffer untuk retry tanpa hit cap.
- Sebelum tiap API call: `bucket.acquire()` blokir sampai token tersedia.

### 5.3 Flow `take_snapshot()`

```
1. INSERT snapshots (taken_at=NOW, status='in_progress')
   → simpan snapshot_id
2. fail_count = 0; total = len(hotkeys)
3. For each hotkey di config (sequential):
     a. rate_limiter.acquire()         # blokir sesuai bucket (max ~15s)
     b. Call TaoStatsClient.get_neuron(subnet_id=56, hotkey=ss58)
        - 200 + neuron data            → INSERT (is_registered=TRUE, uid, emission)
        - 200 + "not registered" / 404 → INSERT (is_registered=FALSE, NULL, NULL)
        - 429 rate-limited (defensive) → tunggu 60s, retry 1×, lalu fail
        - 5xx / timeout                → retry 2× backoff 5s/10s
        - 4xx lain                     → log error, fail_count++
4. UPDATE snapshots SET
     status = CASE
       WHEN fail_count = 0           THEN 'ok'
       WHEN fail_count < total       THEN 'partial'
       ELSE                                'failed'
     END,
     block_number = <ambil dari last successful response kalau ada>
5. Log ringkasan: "Snapshot #N done in MM:SS — X ok, Y deregistered, Z fail"
```

Snapshot duration: ~22 × 15s ≈ **5.5 menit**. Interval 72 menit, jadi tidak overlap.

### 5.4 TaoStats API client (`taostats_client.py`)

- Kelas tipis di atas `httpx.Client`. 1 metode publik: `get_neuron(subnet_id, hotkey) -> NeuronInfo | None`.
- `NeuronInfo`: Pydantic model dengan field minimal: `uid: int`, `emission: float`, `block: int | None`.
- Endpoint persis akan diverifikasi saat implementasi (cek `/reference` dokumentasi TaoStats; coba lewat curl dulu kalau perlu).
- API key di-inject via header. Base URL & header name di-set dari config.
- Return `None` kalau hotkey tidak terdaftar di subnet.

### 5.5 Error handling matrix

| Kondisi | Aksi | Status snapshot |
|---|---|---|
| 1 hotkey gagal setelah retry | Log, fail_count++ | 'partial' (kalau ada yang sukses) |
| Semua hotkey gagal | Log, fail_count = total | 'failed' |
| DB write gagal | Exception propagate ke APScheduler | Run di-skip; jadwal berikutnya tetap jalan |
| Config invalid saat startup | Exit non-zero | (proses tidak jalan) |

## 6. Web API & Frontend

### 6.1 Routes

| Route | Tipe | Isi |
|---|---|---|
| `GET /` | HTML | Dashboard: list orang + akumulasi + status |
| `GET /person/{name}` | HTML | Detail per orang: 2 chart + breakdown hotkey |
| `GET /api/persons` | JSON | Data dashboard |
| `GET /api/persons/{name}/series` | JSON | Time-series per orang (cumulative + per snapshot) |
| `GET /api/hotkeys/{ss58}/series` | JSON | Time-series 1 hotkey |
| `GET /api/snapshots` | JSON | List snapshot terakhir untuk tabel detail |
| `GET /api/snapshots/latest` | JSON | Snapshot terbaru: id, taken_at, status, block |
| `GET /healthz` | text | "ok" |

### 6.2 Date filter

- Query param di route HTML & API: `?range=<preset>` atau `?from=YYYY-MM-DD&to=YYYY-MM-DD`.
- Preset: `today`, `24h`, `7d`, `30d`, `mtd`, `all` (default).
- Semantik akumulasi: `SUM(emission)` **dalam range terpilih** (bukan balance sampai tanggal).
- Header tabel berubah dinamis: "Akumulasi (7d)" / "Akumulasi (01 Mei – 17 Mei)" / "Akumulasi (all time)".
- Validasi: `from > to` → 400 + flash message; range kosong → tampilkan "Belum ada snapshot di range ini."

### 6.3 Dashboard mock

```
Gradient Subnet Emission Tracker
Last update: 2026-05-17 14:38

Range: [ Last 7 days ▾ ]  Today | 24h | [7d] | 30d | All
Period: 2026-05-10 → 2026-05-17

TOTAL AKUMULASI (7d): 312.4 alpha
Aktif: 20  •  🔴 Deregistered: 2

Name      Akumulasi(7d)  ↗ Last snap  Hotkey 1   Hotkey 2
Alice     38.2           +9.21        🟢          🟢
Bob     36.5           +8.95        🟢          🟢
Carol    8.4            +0.00        🔴          🔴  ⚠
...
```

- `↗ Last snap` = nilai emisi di snapshot terakhir (delta yang baru ditambah).
- Status badge per hotkey: 🟢 active, 🔴 deregistered.
- Per orang: warning ⚠ kalau ada hotkey baru deregister di snapshot terakhir.
- Auto-refresh tiap 5 menit via `<meta http-equiv="refresh">`.

### 6.4 Detail per orang (`/person/{name}`)

Berisi:
- Header: nama, akumulasi total dalam range, periode tracking.
- Chart 1: cumulative line chart (3 series: hotkey 1 cum, hotkey 2 cum, total cum).
- Chart 2: per-snapshot bar chart (raw nilai tiap snapshot — biar bisa spot anomali).
- Tabel: 20 snapshot terakhir dengan kolom waktu, hotkey 1, hotkey 2, total, status.
- Filter date sama dengan dashboard.

### 6.5 SQL queries kunci

```sql
-- Total akumulasi per orang dalam range:
SELECT p.name,
       COALESCE(SUM(ns.emission), 0) AS cumulative,
       MAX(CASE WHEN s.id = :latest_snap_id THEN ns.emission END) AS last_snap_emission
FROM persons p
LEFT JOIN hotkeys h           ON h.person_id = p.id
LEFT JOIN neuron_snapshots ns ON ns.hotkey_ss58 = h.ss58
JOIN snapshots s              ON s.id = ns.snapshot_id
WHERE s.status IN ('ok', 'partial')
  AND s.taken_at >= :from_dt
  AND s.taken_at <  :to_dt
GROUP BY p.name
ORDER BY cumulative DESC;

-- Time-series cumulative per hotkey:
SELECT s.taken_at,
       ns.hotkey_ss58,
       ns.emission,
       SUM(ns.emission) OVER (
         PARTITION BY ns.hotkey_ss58
         ORDER BY s.taken_at
       ) AS cumulative
FROM neuron_snapshots ns
JOIN snapshots s ON s.id = ns.snapshot_id
WHERE ns.hotkey_ss58 = :ss58
  AND s.status IN ('ok', 'partial')
  AND s.taken_at >= :from_dt
  AND s.taken_at <  :to_dt
ORDER BY s.taken_at;
```

Snapshot dengan `status='failed'` di-exclude dari akumulasi (data tidak dipercaya).

### 6.6 Frontend stack

- Jinja2 templates (server-side render).
- Chart.js dari CDN.
- Pico.css (classless, ~10KB) untuk styling minimal.
- Tidak ada build step, tidak ada SPA.

## 7. Project Layout

```
data-emmision/
├── .env                       # gitignored
├── .env.example
├── .gitignore
├── README.md
├── pyproject.toml
├── config.yaml
├── config.example.yaml
├── data/                      # gitignored
│   └── emissions.db
├── docs/
│   └── superpowers/specs/
│       └── 2026-05-17-emission-tracker-design.md
├── src/emission_tracker/
│   ├── __init__.py
│   ├── main.py                # FastAPI app factory + entry
│   ├── config.py              # AppConfig pydantic model
│   ├── db.py                  # SQLite connection + schema init
│   ├── taostats_client.py
│   ├── rate_limiter.py
│   ├── bot/
│   │   ├── scheduler.py
│   │   └── snapshot.py
│   └── web/
│       ├── routes_api.py
│       ├── routes_pages.py
│       ├── queries.py
│       ├── templates/
│       │   ├── base.html
│       │   ├── dashboard.html
│       │   └── person.html
│       └── static/style.css
└── tests/
    ├── conftest.py
    ├── test_config.py
    ├── test_rate_limiter.py
    ├── test_taostats_client.py
    ├── test_snapshot.py
    └── test_queries.py
```

## 8. Dependencies

```toml
[project]
name = "emission-tracker"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "httpx>=0.27",
    "apscheduler>=3.10",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "pyyaml>=6.0",
    "jinja2>=3.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.20",
    "freezegun>=1.4",
]
```

SQLite via stdlib `sqlite3` (sync). Untuk skala 22 hotkey × 20 snapshot/hari, sync sudah lebih dari cukup.

## 9. Migrations, Logging, Ops

### 9.1 Schema migration

- Schema di-define sebagai konstanta SQL di `db.py`.
- Startup: jalankan semua `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`.
- Tidak pakai Alembic — overkill untuk schema sekecil ini.
- Migrasi future: tambahkan SQL berurutan di list yang dieksekusi saat startup (idempotent).

### 9.2 Logging

- stdlib `logging`, format `{asctime} {levelname} {name}: {message}`.
- Default level `INFO`; override via env `LOG_LEVEL=DEBUG`.
- Per snapshot: 1 log INFO summary; DEBUG line per hotkey.
- Bot & web share root logger (1 proses).

### 9.3 Run commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env             # isi TAOSTATS_API_KEY
cp config.example.yaml config.yaml

uvicorn emission_tracker.main:app --host 0.0.0.0 --port 8000
uvicorn emission_tracker.main:app --reload    # dev
pytest -v                                      # tests
```

### 9.4 `.gitignore`

```
.venv/
__pycache__/
*.pyc
.env
data/
.pytest_cache/
.coverage
```

## 10. Testing Strategy

| Module | Yang di-test | Cara |
|---|---|---|
| `config.py` | Validasi SS58, unique names, no duplicate hotkey | Param tests dengan pydantic |
| `rate_limiter.py` | 5 token / 60 detik; blokir kalau habis | `freezegun` mock time |
| `taostats_client.py` | Parse response, handle 404 (not registered), 5xx retry | `respx` mock httpx |
| `bot/snapshot.py` | Full flow: ok/partial/failed status, deregister detection | Temp SQLite + mocked client |
| `web/queries.py` | Akumulasi math, filter tanggal, exclude failed snapshots | Seed DB → assert hasil query |
| `web/routes_*.py` | Endpoint return 200, JSON shape benar | FastAPI TestClient |

Target coverage: ~80% pada logic modules. Templates & route wiring tidak perlu unit test.

## 11. Decisions Log

| # | Decision | Rationale |
|---|---|---|
| 1 | Scope = subnet 56 only, 22 hotkey, 11 orang | User explicit |
| 2 | Polling per tempo (72 menit) | Selaras dengan distribusi emisi di chain |
| 3 | Single process (FastAPI + APScheduler) | Scope kecil, simple deploy |
| 4 | SQLite (stdlib sync) | Volume data kecil; tidak butuh server DB |
| 5 | Hotkey-level granularity (bukan top-N) | User pilih hotkey spesifik |
| 6 | Akumulasi = SUM per snapshot di range | User request eksplisit |
| 7 | Rate limit: 15s antar req (~4/min) | Buffer di bawah cap 5/min untuk retry |
| 8 | Deregistered → insert row dengan is_registered=FALSE | Jejak historis preserved |
| 9 | Server-rendered (Jinja2 + Chart.js), bukan SPA | Simple, tidak ada build step |
| 10 | Date filter via URL query param | Bookmark/share friendly, no JS state |

## 12. Open Items (untuk verifikasi saat implementasi)

1. **Endpoint TaoStats API yang persis** untuk query neuron per subnet+hotkey. Akan dicek di `https://docs.taostats.io/reference` saat coding.
2. **Semantik field `emission`**: apakah delta per tempo (cocok untuk SUM akumulasi) atau rate. Kalau rate, perlu pilih field/endpoint berbeda.
3. **Format error response** TaoStats saat hotkey tidak terdaftar di subnet (404? 200 dengan empty array? Specific error code?).
4. **Header name untuk API key** (kemungkinan `Authorization` atau `x-api-key`).
