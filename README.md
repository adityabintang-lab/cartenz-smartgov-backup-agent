# SmartGOV Backup Agent

Agent di server target: dump MariaDB (+ opsional berkas aplikasi) → `tar.gz` → enkripsi **age** → unggah ke Object Storage lewat presigned URL dari Central Gateway. Dashboard web untuk setup, status, dan backup manual.

## Instalasi cepat (server agent)

```bash
curl -fsSLO https://raw.githubusercontent.com/adityabintang-lab/cartenz-smartgov-backup-agent/main/install.sh
sudo bash install.sh                 # atau: sudo bash install.sh --bind 0.0.0.0  (hanya di belakang HTTPS)
```

Skrip meng-clone/menarik kode terbaru, membuat folder + permission, membuat network `smartgov-network` bila belum ada,
**membangun image dari kode di server** (`up -d --build`), menunggu agent sehat, lalu menampilkan token setup.
Aman dijalankan ulang untuk update. Catatan: skrip mengambil kode dari GitHub, jadi perubahan harus sudah di-push.

## Menjalankan manual

```bash
mkdir -p agent_data backups logs secrets
sudo chown -R 1000:1000 agent_data backups logs secrets   # container berjalan sebagai uid 1000
docker compose up -d --build
docker logs cartenz-smartgov-backup-agent                  # salin TOKEN SETUP dari sini
```

Dashboard: `http://127.0.0.1:25200` (default hanya localhost). Akses remote: SSH tunnel
(`ssh -L 25200:127.0.0.1:25200 server`) atau reverse proxy HTTPS. Untuk membuka ke jaringan:
`AGENT_BIND_ADDR=0.0.0.0` di `.env` — **hanya** di belakang TLS, karena dashboard memakai HTTP Basic.

Setup pertama membutuhkan **token setup** (dicetak di log container) sehingga orang lain di jaringan tidak bisa
merebut agent sebelum Anda. Setelah setup, semua perubahan lewat *Pengaturan* di dashboard (butuh login).

## Enkripsi & pemulihan

Buat keypair **di komputer Anda**, bukan di server:

```bash
age-keygen -o kunci-backup.txt      # simpan file ini OFFLINE (password manager / brankas)
# salin baris "Public key: age1..." ke Pengaturan > Public key age
```

Jika kolom itu dikosongkan, agent membuat keypair di `secrets/` dan dashboard menampilkan peringatan: private key
yang tersimpan di server yang sama dengan backup berarti penyerang yang menguasai server bisa membuka semuanya.

Pemulihan:

```bash
age -d -i kunci-backup.txt smartgov_backup_XXXX.tar.gz.age | tar -xz -C ./restore
mariadb -h HOST -u USER -p NAMA_DB < ./restore/database.sql
```

## Catatan operasional

- Jadwal mengikuti *Interval backup*; state tersimpan di `agent_data/backup_state.json`, jadi restart container
  tidak memicu backup ulang. Backup gagal dicoba lagi paling lama 1 jam kemudian.
- Status akhir: **Berhasil**, **Arsip lokal OK, unggah gagal** (exit 2), atau **Gagal**.
- Berkas aplikasi ikut dibackup bila Anda mount ke `/app/smartgov_app` (lihat contoh di `docker-compose.yml`).
- Jangan menaikkan `--workers` uvicorn: penjadwal dan lock berjalan per-proses.
- `agent_data/agent_config.json` (0600) memuat password DB dan token agent dalam bentuk plaintext karena
  dibutuhkan untuk menjalankan backup; lindungi direktorinya. Password admin disimpan sebagai hash scrypt.
