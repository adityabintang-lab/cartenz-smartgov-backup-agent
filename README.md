# SmartGOV Backup Agent

Agent di server target membackup **semua database MariaDB** dan, bila dipasang, berkas aplikasi ke dalam arsip terenkripsi **age** yang terpisah. Arsip lokal diunggah ke Object Storage melalui presigned URL dari Central Gateway. Dashboard menyediakan setup, status, konfigurasi jadwal, retensi, dan backup manual.

## Instalasi cepat (server agent)

```bash
curl -fsSLO https://raw.githubusercontent.com/adityabintang-lab/cartenz-smartgov-backup-agent/main/install.sh
sudo bash install.sh                 # atau: sudo bash install.sh --bind 0.0.0.0  (hanya di belakang HTTPS)
```

Skrip meng-clone/menarik kode terbaru, membuat folder + permission, membuat network `smartgov-network` bila belum ada,
**membangun image dari kode di server** (`up -d --build`), menunggu agent sehat, lalu menampilkan token setup.
Aman dijalankan ulang untuk update. Catatan: skrip mengambil kode dari GitHub, jadi perubahan harus sudah di-push.

Build memakai repository Debian melalui HTTPS, retry APT/PyPI untuk gangguan sementara, dan memaksa IPv4 untuk mengakomodasi host/Docker network yang tidak memiliki konektivitas IPv6. Jika mirror default tidak dapat dijangkau dari jaringan server, sebelum menjalankan ulang installer tambahkan mirror HTTPS yang tersedia di `.env`:

```dotenv
APT_MIRROR=https://mirror.example.go.id
PIP_INDEX_URL=https://pypi.org/simple
```

`APT_MIRROR` harus berupa root mirror Debian tanpa akhiran `/debian`; Compose menambahkan path repository Debian secara otomatis. Installer mempertahankan pengaturan lain di `.env` saat opsi `--bind` digunakan. Setelah build gagal, aman untuk menjalankan ulang `sudo bash install.sh`; tidak perlu menghapus container, image, atau data backup. Jika tetap gagal, periksa DNS dan akses keluar container Docker ke HTTPS port 443, proxy/firewall, lalu coba mirror HTTPS lain yang dekat dengan server. Jangan menonaktifkan verifikasi sertifikat TLS.

## Menjalankan manual

```bash
mkdir -p agent_data backups logs secrets
sudo chown -R 1000:1000 agent_data backups logs secrets   # container berjalan sebagai uid 1000
docker compose up -d --build
docker logs cartenz-smartgov-backup-agent                  # salin TOKEN SETUP dari sini
```

Dashboard: `http://127.0.0.1:25200` (default hanya localhost). Akses remote: SSH tunnel
(`ssh -L 25200:127.0.0.1:25200 server`) atau reverse proxy HTTPS. Untuk membuka ke jaringan:
`AGENT_BIND_ADDR=0.0.0.0` di `.env` — utamakan TLS; tanpa TLS, batasi ke jaringan internal/VPN dan firewall ketat. Login dashboard memakai cookie sesi `HttpOnly` dan `SameSite=Strict`; atribut `Secure` dipasang saat akses HTTPS. API juga tetap menerima HTTP Basic untuk kompatibilitas. Session berlaku 8 jam dan hilang saat Agent restart.

Dashboard menyediakan pilihan mode terang/gelap dan menyimpan pilihan di browser. Font Plus Jakarta Sans disajikan lokal dari `assets/fonts/`, sehingga browser tidak perlu menghubungi font host pihak ketiga. Lisensi font tersimpan bersama font pada `assets/fonts/OFL.txt`.

Dashboard tetap dapat dipakai melalui HTTP untuk lingkungan yang belum memiliki TLS; pada HTTPS cookie diberi atribut `Secure`, sedangkan pada HTTP atribut tersebut tidak dipasang agar sesi tetap berfungsi. HTTP tidak mengenkripsi password, cookie, maupun data dashboard. Batasi penggunaan HTTP ke jaringan internal/VPN tepercaya dan firewall; gunakan HTTPS bila memungkinkan.

Agent juga menolak path probe umum seperti `/.env`, `/.git`, `wp-admin`, dan `cgi-bin` dengan 404 serta mengirim header keamanan. Ini bukan pengganti firewall atau TLS: pertahankan port Agent di localhost bila memungkinkan, batasi akses melalui firewall, dan gunakan HTTPS reverse proxy untuk akses jaringan. Pertahankan access log untuk mendeteksi pemindaian.

Setup pertama membutuhkan **token setup** (dicetak di log container) sehingga orang lain di jaringan tidak bisa
merebut agent sebelum Anda. Wizard setup muncul sebelum konfigurasi; setelah setup, dashboard menampilkan form login dan semua perubahan lewat *Pengaturan* memerlukan login.

## Pemisahan file backup

Database dan aplikasi disimpan sebagai arsip terpisah:

- Lokal: `backups/database/` dan `backups/application/`.
- COS: `backups/<nama-daerah-agent>/database/` dan `backups/<nama-daerah-agent>/application/`.

Nama folder daerah berasal dari nama server/token yang dibuat di dashboard Gateway. Contohnya, token bernama `Kabupaten Bogor` menghasilkan folder `backups/kabupaten_bogor/`. Nama token yang menghasilkan folder sama ditolak agar arsip dua Agent tidak tercampur.

Dump database memakai `mariadb-dump --all-databases` serta menyertakan routines, triggers, dan events. Akun MariaDB perlu memiliki izin membaca database yang ingin dicadangkan.

### Memilih folder aplikasi dari UI

Container Agent hanya dapat membaca folder host yang sudah di-mount. Di tiap server, atur satu root host di file `.env` pada folder Agent, lalu rebuild/restart Agent:

```dotenv
APP_SOURCE_HOST_ROOT=/opt/cartenz/teknolab/smartgov
```

Buat direktori root tersebut di host, lalu jalankan `docker compose up -d --build` dari folder Agent. Mount tersedia read-only di container sebagai `/backup_sources`. Di Pengaturan Agent isi **Subfolder aplikasi** relatif terhadap root, misalnya `revenue`. Jika root server lain adalah `/home/cartenz/teknolab/smartgov`, atur nilai `.env` server itu ke path tersebut dan isi subfolder UI yang sama. Berikan user container (UID 1000) izin baca dan traverse pada folder serta file yang dipilih. Kosongkan kolom UI untuk menonaktifkan backup aplikasi.

Path UI dibatasi ke subfolder di dalam root yang di-mount; path absolut dan traversal ke luar root ditolak.

## Jadwal dan retensi

Di Pengaturan Agent, pilih salah satu pola:

- **Interval:** setiap N menit atau jam (minimum 1 menit).
- **Harian:** pada jam dan menit yang ditentukan.
- **Bulanan:** pada tanggal dan jam tertentu; bila tanggal tersebut tidak ada pada bulan itu, backup berjalan pada hari terakhir bulan.

Jadwal harian dan bulanan mengikuti zona waktu lokal server. Backup kalender pertama menunggu jadwal berikutnya; backup pertama dengan mode interval langsung dijalankan. Backup manual tetap tersedia dari dashboard.

Batas awal per Agent adalah **7 arsip database** dan **2 arsip aplikasi**, baik di disk Agent maupun di COS. Batas dapat diubah di Pengaturan Agent. File lokal tertua dihapus setelah arsip baru dibuat; file COS tertua dihapus setelah arsip baru berhasil diunggah. Jika upload atau penghapusan di COS gagal, kegagalan ditampilkan di log dan file lokal tetap tersedia. Status dashboard membedakan kegagalan upload dari kegagalan rotasi COS. Berikan kredensial COS izin menghapus objek pada prefix backup agar rotasi jarak jauh berfungsi.

Arsip di atas 4 GiB diunggah sebagai multipart dalam bagian 64 MiB (ukuran bagian menyesuaikan untuk arsip yang sangat besar). Bagian yang gagal dicoba ulang dan unggahan multipart dibatalkan bila proses gagal; arsip lokal tetap tersedia. Agent memberi proses backup waktu maksimum 24 jam secara default. Atur `BACKUP_TIMEOUT` di `.env` lebih tinggi jika kecepatan jaringan membuat transfer memerlukan waktu lebih lama. Sebagai perlindungan tambahan jika container mati paksa, aktifkan aturan siklus hidup COS untuk menghapus multipart upload yang tidak selesai.

Untuk menguji upload arsip lokal tanpa menjalankan ulang kompresi/enkripsi:

```bash
docker exec cartenz-smartgov-backup-agent python3 /app/scripts/cos.py upload application "/app/backups/application/NAMA_FILE.tar.gz.age"
```

Ganti kategori/path sesuai arsip. Perintah ini mengunggah ke COS tanpa menjalankan retensi; nama file yang sama menargetkan objek COS yang sama.

Arsip gabungan dari versi lama yang berada langsung di root `backups/` lokal atau `backups/<daerah>/` COS dipertahankan dan tidak ikut rotasi kategori otomatis.

## Enkripsi & pemulihan

Buat keypair **di komputer Anda**, bukan di server:

```bash
age-keygen -o kunci-backup.txt      # simpan file ini OFFLINE (password manager / brankas)
# salin baris "Public key: age1..." ke Pengaturan > Public key age
```

Jika kolom itu dikosongkan, agent membuat keypair di `secrets/` dan dashboard menampilkan peringatan: private key
yang tersimpan di server yang sama dengan backup berarti penyerang yang menguasai server bisa membuka semuanya.

Pemulihan database:

```bash
mkdir -p ./restore/database
age -d -i kunci-backup.txt backups/database/smartgov_database_XXXX.tar.gz.age | tar -xz -C ./restore/database
mariadb -h HOST -u USER -p < ./restore/database/database.sql
```

Pemulihan berkas aplikasi:

```bash
mkdir -p ./restore/application
age -d -i kunci-backup.txt backups/application/smartgov_application_XXXX.tar.gz.age | tar -xz -C ./restore/application
```

## Catatan operasional

- State jadwal tersimpan di `agent_data/backup_state.json`, jadi restart container tidak memicu backup kalender ulang. Backup gagal dicoba lagi paling lama satu jam kemudian.
- Status akhir: **Berhasil**, **Arsip lokal OK, unggah gagal** (exit 2), **Terunggah, rotasi COS gagal** (exit 3), atau **Gagal**.
- Berkas aplikasi ikut dibackup bila subfoldernya diatur di Pengaturan Agent dan tersedia di root host yang di-mount.
- Jangan menaikkan `--workers` uvicorn: penjadwal dan lock berjalan per-proses.
- `agent_data/agent_config.json` (0600) memuat password DB dan token agent dalam bentuk plaintext karena
  dibutuhkan untuk menjalankan backup; lindungi direktorinya. Password admin disimpan sebagai hash scrypt.
