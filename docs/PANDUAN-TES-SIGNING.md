# Panduan tes fitur signing

Untuk dijalankan sekali, saat pertama kali mempercayai tombol Pay fee dan
Unstake all. Urutannya sengaja: tiap langkah membuktikan satu hal, dan yang
memindahkan dana ada di paling akhir.

Buka dashboard, **hard refresh** dulu (Cmd+Shift+R / Ctrl+Shift+R) — CSS dan
JS berubah banyak selama pengembangan.

---

## Tes 1 — rantai penuh, nol risiko

**Tujuan:** membuktikan browser → aplikasi → signer → btcli tersambung, dan
kegagalan ditangani dengan benar.

1. Pilih kartu yang saldonya cukup — cari **Wallet ≥ 0.40 τ**. Biasanya
   Ilhamr, Firza I, Yosa, atau Dodi.
2. Centang **image (0.4 τ)** saja. Total di atas tombol harus terbaca
   `0.40 τ`, dan tombol **Pay fee** hidup.
3. Klik **Pay fee**. Dialog muncul, isi kolom unlock value dengan teks
   asal — misalnya `salah`.
4. Klik **Kirim**.

**Yang harus terjadi:**

| Kapan | Tampilan |
|---|---|
| Seketika | Strip di kartu jadi `● sedang proses…` biru |
| ~1–3 detik | Strip jadi `✕ gagal — …` merah |
| Bersamaan | Panel **Riwayat aksi** bertambah satu baris berstatus `GAGAL` |

Hover strip merahnya untuk melihat pesan lengkap. Di panel riwayat, kolom
Keterangan sengaja dipotong — hover untuk teks penuh.

Pesan yang diharapkan: `btcli reported success=false`. btcli memang tidak
memberi alasan saat menolak membuka kunci; itu keterbatasan btcli, bukan
bug. Pesan itu hampir selalu berarti unlock value salah.

**Nol dana bergerak.** Kalau ketiga baris tabel di atas muncul, seluruh
rantai sudah benar termasuk jalur kegagalannya.

### Kalau yang muncul bukan itu

| Yang kamu lihat | Artinya |
|---|---|
| `Balance 0.0000 τ is short of 0.4000 τ` | Pembacaan saldo wallet itu gagal (429 dari TaoStats). Klik ⟳ di kartu itu, tunggu, coba lagi. |
| `signer unreachable` | Service signer mati. `sudo systemctl status emission-signer` |
| `An action for this coldkey is already running` | Masih ada aksi menggantung. Tunggu maksimal 90 detik, ia akan berhenti sendiri. |
| Menggantung > 90 detik | Laporkan — ini hang yang belum pernah berhasil direproduksi. |

---

## Tes 2 — pembayaran sungguhan

**Ini memindahkan uang nyata di mainnet.** 0.4 τ ≈ $95. Lakukan hanya kalau
kamu memang berniat membayar fee turnamen image untuk wallet itu.

Sama persis seperti Tes 1, tapi isi unlock value **wallet itu yang asli**.

**Yang harus terjadi:** strip jadi hijau `✓ fee berhasil (0.40 τ)`, panel
riwayat menampilkan status `BERHASIL`, dan kolom Keterangan berisi **tx
hash** — bukan kosong. Halaman reload sendiri setelah ~1 detik supaya saldo
wallet ikut diperbarui.

Kalau statusnya berhasil tapi **tx hash kosong**, berhenti dan laporkan.
Itu pola yang pernah menandakan bug "gagal terbaca sebagai berhasil"; sudah
diperbaiki, tapi tx hash kosong tetap bukan bukti pembayaran.

Verifikasi independen — jangan hanya percaya dashboard:

```bash
sudo -u signer env HOME=/tmp /usr/local/bin/btcli wallet balance \
  --wallet-name <nama wallet> --wallet-path /root/.bittensor/wallets --json-output
```

Saldo `free` harus berkurang ~0.4 τ plus biaya transaksi.

---

## Tes 3 — cakupan unstake

**Jangan pakai tombol Unstake all sebelum langkah ini selesai.**

Yang belum pernah dibuktikan siapa pun: apakah `--unstake-all --netuid 56`
benar-benar terbatas pada subnet 56, atau menjangkau stake di subnet lain.
Beberapa coldkey punya posisi kecil di netuid 24.

Jalankan manual di VPS, **dengan prompt menyala** supaya kamu bisa membaca
dan membatalkan:

```bash
sudo -u signer env HOME=/tmp \
  BT_PW__ROOT__BITTENSOR_WALLETS_GOY_COLDKEY='<unlock value wallet goy>' \
  /usr/local/bin/btcli stake remove --unstake-all --netuid 56 --all-hotkeys \
      --safe-staking --tolerance 0.05 --allow-partial-stake \
      --wallet-name goy --wallet-path /root/.bittensor/wallets
```

Goy dipilih karena stake-nya paling kecil (0.24 α), jadi kalau pun terlanjur
berjalan, kerugiannya minimal.

**Baca daftar yang akan di-unstake sebelum mengkonfirmasi.**

- Kalau semuanya netuid 56 → aman, tombol Unstake all boleh dipakai.
- Kalau ada subnet lain di daftar → **batalkan**, dan `unstake_argv` di
  `src/emission_tracker/signer/btcli.py` harus ditulis ulang sebelum
  tombolnya dipakai.

---

## Kalau ingin membersihkan riwayat percobaan

Baris-baris `GAGAL` dari masa pengembangan bukan pembayaran sungguhan.
Menghapusnya tidak menghilangkan apa pun yang nyata:

```bash
sudo -u emission sqlite3 /opt/emission-tracker/data/emissions.db \
  "DELETE FROM signed_actions;"
```

Lakukan **sebelum** Tes 2, supaya catatan pembayaran sungguhanmu bersih
sejak baris pertama.

---

## Yang perlu diketahui, bukan bug

**Snapshot `failed` beruntun pada 11 September ~09:27–09:54** adalah artefak
restart berulang saat pemasangan. Tiap restart memulai snapshot baru dan
restart berikutnya membunuhnya. Snapshot normal terakhir sebelum itu,
#2324, hasilnya `35 ok, 1 deregistered, 0 fail`.

**Sebagian pembacaan saldo bisa gagal (429).** TaoStats membatasi laju, dan
snapshot emisi berbagi jatah yang sama. Wallet yang gagal dibaca tampil `—`,
bukan angka basi. Klik ⟳ di kartunya untuk membaca ulang satu wallet saja.

**Tombol Pay fee mati di sebagian besar kartu.** Itu benar — saldonya memang
di bawah fee termurah. Isi wallet-nya, atau unstake dulu setelah Tes 3.
