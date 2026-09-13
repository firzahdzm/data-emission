# Wallet induk: penyatuan dana dan distribusi

**Tanggal:** 2026-09-14
**Status:** disetujui untuk diimplementasikan

## Masalah

Coldkey `5HERhLCKSpmTiRD6EpnsY7DUnVqUThaANhYgXYAWqZZ28fLB` (di dashboard:
"Bersama") adalah wallet induk tim: tempat semua hasil emisi dikumpulkan,
dan sumber dana untuk registrasi turnamen tiap anggota.

Hari ini kedua arah itu dikerjakan manual lewat `btcli` di server. Yang
sudah ada di dashboard hanya pembayaran fee turnamen — satu tujuan tetap,
jumlah dari tabel biaya — dan unstake. Perpindahan dana antar wallet
sendiri belum punya jalur sama sekali.

## Yang dibangun

Dua tombol di panel wallet dashboard, keduanya hanya untuk admin:

1. **Penyatuan dana** — tiap coldkey anggota mengirim saldo bebasnya ke
   wallet induk, menyisakan 0,015 τ.
2. **Distribusi** — wallet induk mengirim sejumlah TAO ke coldkey yang
   dipilih, jumlahnya diketik admin per wallet.

Uang hanya bergerak saat admin menekan tombol. Tidak ada jadwal, tidak
ada otomatisasi.

## Keputusan desain

### Tujuan transfer dikunci ke roster

Signer menolak transfer ke alamat mana pun di luar roster coldkey yang
ada di confignya (`hotkeys:` sudah memuat pemetaan coldkey → hotkey; kunci
dari peta itulah daftar coldkey yang sah).

Ini pengganti plafon jumlah, dan lebih kuat daripada plafon. Sampai
sekarang signer aman karena tujuannya satu alamat tetap dan jumlahnya dari
tabel biaya; distribusi membongkar keduanya. Dengan tujuan terkunci,
skenario terburuk dari web yang dibobol adalah **uang berpindah di antara
wallet tim sendiri** — berantakan, tapi tidak hilang. Tidak ada plafon
jumlah, atas permintaan eksplisit pemilik sistem; gantinya dua pop up
konfirmasi berturut-turut.

### Jumlah sapuan dihitung signer, bukan web

Permintaan penyatuan dana hanya menyebut **wallet mana**. Signer membaca
saldo wallet itu dari chain saat eksekusi (`btcli wallet balance
--json-output`, tanpa password) lalu mengirim `saldo_bebas − 0,015 τ`.

Sebabnya: saldo di dashboard dibaca sekali sehari dari TaoStats dan
terbukti bisa meleset jauh — pekan ini sebuah kartu menampilkan stake 0,71
τ pada wallet yang sudah kosong. Kalau web yang menghitung jumlah, angka
basi itu langsung jadi transfer yang salah atau ditolak chain. Dengan
signer yang membaca sendiri, angka basi paling jauh hanya membuat daftar
di layar kurang tepat.

Sisa 0,015 τ tetap tinggal supaya tiap coldkey punya ongkos untuk
transaksi berikutnya; biaya transaksi (~0,0001–0,003 τ) dipotong dari
jumlah yang dikirim, jadi sisanya sedikit di bawah 0,015 τ. Angkanya
diatur di config signer (`sweep_leave_tao`).

### Satu permintaan signer per transfer

Web mengulang: 14 permintaan untuk penyatuan dana, satu per penerima untuk
distribusi, semuanya berurutan.

Bukan satu permintaan berisi seluruh rencana. Tiap transfer mendapat baris
audit sendiri, kunci "sedang berjalan" sendiri, dan hasil sendiri,
sehingga "berhasil sebagian" jadi keadaan yang bisa dibaca dan
dilanjutkan. Pekan ini dua masalah paling mahal justru berasal dari operasi
gabungan: batch unstake atomik yang jatuh seluruhnya karena satu penolakan,
dan extrinsic terlindungi yang hasilnya tidak diketahui.

### Unlock value

Semua wallet memakai unlock value yang sama. Pop up meminta satu kali per
klik, nilainya dikirim bersama tiap permintaan, tidak pernah disimpan di
disk maupun di environment — sama seperti jalur pembayaran dan unstake.

## Perubahan per komponen

### `signer/protocol.py`

`SignRequest` mendapat dua field opsional:

- `destination: str = ""` — hanya untuk `distribute`; wajib ada di roster.
- `amount_rao: int = 0` — hanya untuk `distribute`.

Operasi baru: `OP_SWEEP = "sweep"`, `OP_DISTRIBUTE = "distribute"`.
`from_line` menolak: op tak dikenal; `amount_rao`/`destination` pada op
yang tidak memakainya; `amount_rao <= 0` untuk `distribute`; destination
bukan string ss58.

### `signer/btcli.py`

- `balance_argv(wallet_name, wallet_path)` → `btcli wallet balance
  --wallet-name … --wallet-path … --json-output`.
- `free_balance_rao(payload, coldkey)` — ambil `balances.<wallet>.free`,
  ubah ke rao lewat `round(x * 1e9)`; kembalikan `None` kalau tidak ada.
  Tidak pernah menebak nol: nol berarti "kosong", tidak ada berarti "tidak
  terbaca", dan keduanya menghasilkan tindakan berbeda.
- `transfer_argv` dipakai ulang apa adanya untuk kedua operasi.

### `signer/server.py`

`SignerConfig` mendapat:

- `parent_coldkey: str = ""` — tujuan penyatuan dana.
- `sweep_leave_tao: float = 0.015`.

`Signer._handle` menangani dua op baru:

**`sweep`** — coldkey pengirim harus ada di roster; wallet induk sendiri
ditolak (tidak menyapu ke dirinya sendiri). Baca saldo; kalau
`free ≤ sweep_leave_tao` kembalikan gagal dengan alasan "saldo di bawah
ambang" (bukan error, dan bukan "tidak pasti"). Kirim `free − leave` ke
`parent_coldkey` lewat `run_btcli_pty` + `parse_transfer_output`, jalur
yang sama dengan pembayaran fee.

**`distribute`** — pengirim harus `parent_coldkey`; `destination` harus ada
di roster dan bukan induk itu sendiri; `amount_rao > 0`. Kirim.

Keduanya memakai `TRANSFER_TIMEOUT`, dan mencatat transkrip lengkap ke log
kalau hasilnya tidak jelas berhasil — sama seperti unstake.

### `db.py`

Kolom baru `counterparty_ss58 TEXT` pada `signed_actions`, ditambahkan
lewat migrasi idempoten seperti kolom-kolom sebelumnya. Tanpa itu, semua
baris distribusi tertulis atas nama 5HER dan tidak ada catatan uangnya ke
siapa.

`record_action` menerima `counterparty` opsional. `CHECK` pada kolom
`status` tidak berubah; nilai `op` tidak dibatasi CHECK, jadi op baru tidak
perlu migrasi tambahan.

### `web/routes_api.py`

- `POST /api/treasury/sweep/{coldkey}` — body: `secret`. Admin saja.
- `POST /api/treasury/distribute/{coldkey}` — body: `secret`,
  `amount_rao`. Admin saja. `{coldkey}` adalah **penerima**; pengirim
  selalu wallet induk dan tidak pernah datang dari web.
- `GET /api/treasury/balances` — saldo bebas terkini semua coldkey roster
  lewat signer (btcli, bukan TaoStats). Admin saja. Dipakai untuk mengisi
  kedua pop up.

Ketiganya lewat `_run_signed_action` yang sudah ada, sehingga baris audit,
kunci "satu aksi per coldkey", penanganan 409 untuk hasil tidak pasti, dan
502 untuk gagal berlaku otomatis.

Pembacaan saldo butuh op signer sendiri (`balances`) yang tidak
menandatangani apa pun dan tidak menerima unlock value.

### `web/templates/dashboard.html`

Dua tombol di header panel wallet, di sebelah "Refresh balances" dan
"Unstake terpilih":

- **Satukan dana** → ambil `/api/treasury/balances` → pop up daftar wallet
  di atas ambang: nama, saldo, jumlah yang akan dikirim, total di bawah →
  ketik `satukan` + unlock value → kirim berurutan → ringkasan.
- **Distribusi** → ambil `/api/treasury/balances` → pop up satu baris per
  coldkey anggota (nama, saldo terkini, kolom jumlah, kosong = lewati) →
  pop up kedua: daftar penerima + total + saldo induk, merah kalau total
  melebihi saldo → ketik `distribusi` + unlock value → kirim berurutan →
  ringkasan.

Pop up bertingkat ini butuh dialog yang lebih kaya daripada `snAuthorize`
yang ada sekarang (satu pesan, satu kolom ketik, satu kolom sandi).
Tambahkan `snForm(rows, opts)` di `base.html` yang membangun daftar kolom
angka dan mengembalikan `{ok, values}`; `snAuthorize` tetap dipakai untuk
langkah konfirmasi keduanya.

Ringkasan akhir memisahkan berhasil / gagal / tidak pasti, persis seperti
unstake massal, dan hasil 409 tidak pernah dihitung sebagai kegagalan.

### `deploy/`

`signer.example.yaml` dan `DEPLOY.md` mendapat `parent_coldkey` dan
`sweep_leave_tao`, beserta catatan bahwa tanpa `parent_coldkey` kedua
operasi ditolak — sehingga deployment yang belum dikonfigurasi gagal
tertutup, bukan terbuka.

## Yang dipagari test

Perilaku, bukan implementasi:

- Transfer ke alamat di luar roster ditolak sebelum btcli dipanggil.
- Distribusi dari wallet selain induk ditolak.
- Jumlah sapuan dihitung dari saldo yang dibaca signer, bukan dari apa pun
  yang dikirim web; permintaan sapuan yang membawa `amount_rao` ditolak
  protokol.
- Saldo tepat di ambang dan di bawah ambang tidak menghasilkan transfer.
- Sisa setelah sapuan adalah `saldo − 0,015 τ` dikirim; bukan `saldo`.
- Saldo yang tidak terbaca (payload kosong/rusak) menghasilkan gagal, bukan
  transfer nol dan bukan crash.
- Kolom kosong di pop up distribusi tidak menghasilkan permintaan.
- Unlock value tidak pernah muncul di argv, di log, atau di badan error
  422.
- Kegagalan satu wallet tidak menghentikan wallet berikutnya; ringkasan
  menyebut jumlah berhasil, gagal, dan tidak pasti secara terpisah.
- Baris audit distribusi menyimpan penerima; tanpa itu riwayat tidak bisa
  menjawab "uangnya ke siapa".
- Endpoint treasury menolak pengguna non-admin.

## Yang sengaja tidak dibangun

- **Tanpa plafon jumlah** — atas keputusan pemilik sistem, digantikan
  penguncian tujuan ke roster dan dua konfirmasi.
- **Tanpa penjadwalan** — uang hanya bergerak saat tombol ditekan.
- **Tanpa pembayaran fee turnamen dari wallet induk** — fee tetap dibayar
  tiap coldkey dari saldonya sendiri, seperti sekarang.
- **Tanpa retry otomatis** — permintaan yang gagal diulang manual oleh
  admin, karena retry otomatis atas transfer adalah cara tercepat membayar
  dua kali.
