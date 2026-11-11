#!/usr/bin/env bash
# Backup SmartGOV: dump MariaDB (+ berkas aplikasi) -> tar.gz -> enkripsi age -> unggah via Central Gateway.
# Exit code: 0 sukses | 1 gagal | 2 arsip lokal OK tetapi unggah gagal | 75 backup lain sedang berjalan
set -Eeuo pipefail
umask 077

CONFIG_FILE="${CONFIG_FILE:-/app/data/agent_config.json}"
BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
LOG_DIR="${LOG_DIR:-/app/logs}"
SECRETS_DIR="${SECRETS_DIR:-/app/secrets}"
APP_SOURCE_DIR="${APP_SOURCE_DIR:-/app/smartgov_app}"
LOCK_FILE="${LOCK_FILE:-/tmp/smartgov-backup.lock}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOG_FILE="${LOG_DIR}/backup.log"
AGE_KEY_FILE="${SECRETS_DIR}/age_key.txt"
AGE_PUB_FILE="${SECRETS_DIR}/age_public_key.txt"
STAGING_ROOT="${BACKUP_DIR}/.staging"   # staging di disk (bukan tmpfs/RAM)

mkdir -p "$BACKUP_DIR" "$LOG_DIR" "$SECRETS_DIR" "$STAGING_ROOT"

log() {
    printf '[%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S%z')" "$1" | tee -a "$LOG_FILE"
}

# Cegah dua backup berjalan bersamaan (jadwal + manual + docker exec)
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "Backup lain sedang berjalan, dilewati."
    exit 75
fi

# Rotasi log sederhana (>5 MB)
if [ -f "$LOG_FILE" ] && [ "$(stat -c %s "$LOG_FILE")" -gt 5242880 ]; then
    mv -f "$LOG_FILE" "${LOG_FILE}.1"
fi

WORK=""
PARTIAL=""
# shellcheck disable=SC2329  # dipanggil via trap
cleanup() {
    if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
    if [ -n "$PARTIAL" ]; then rm -f "$PARTIAL"; fi
}
trap cleanup EXIT
trap 'log "ERROR: backup berhenti di baris $LINENO."' ERR

log "=== MEMULAI PROSES BACKUP SMARTGOV AGENT ==="

if [ ! -f "$CONFIG_FILE" ]; then
    log "ERROR: Konfigurasi agent belum ada di ${CONFIG_FILE}. Selesaikan Setup Wizard terlebih dahulu."
    exit 1
fi

# Baca konfigurasi sekali saja (satu baris per nilai; nilai sudah divalidasi tanpa karakter kontrol)
mapfile -t CFG < <(python3 - "$CONFIG_FILE" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
for key, default in (("db_host", ""), ("db_port", 3306), ("db_user", ""), ("db_pass", ""),
                     ("db_name", ""), ("age_recipient", ""), ("retention_days", 30)):
    print(str(c.get(key, default)).replace("\n", " ").replace("\r", " "))
PY
)
DB_HOST="${CFG[0]:-}"; DB_PORT="${CFG[1]:-3306}"; DB_USER="${CFG[2]:-}"; DB_PASS="${CFG[3]:-}"
DB_NAME="${CFG[4]:-}"; AGE_RECIPIENT="${CFG[5]:-}"; RETENTION_DAYS="${CFG[6]:-30}"

[[ "$DB_PORT" =~ ^[0-9]+$ ]] || DB_PORT=3306
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || RETENTION_DAYS=30

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
ENCRYPTED_NAME="smartgov_backup_${TIMESTAMP}.tar.gz.age"
OUT_PATH="${BACKUP_DIR}/${ENCRYPTED_NAME}"
PARTIAL="${OUT_PATH}.partial"

# Bersihkan sisa staging/partial dari proses yang pernah terhenti paksa
find "$STAGING_ROOT" -mindepth 1 -maxdepth 1 -mtime +1 -exec rm -rf {} + 2>/dev/null || true
find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.partial' -mtime +1 -delete 2>/dev/null || true

WORK="$(mktemp -d -p "$STAGING_ROOT")"
PAYLOAD="${WORK}/payload"
mkdir -p "$PAYLOAD"

# 1. Dump database
if [ -n "$DB_HOST" ] && [ -n "$DB_NAME" ]; then
    log "Dump database '${DB_NAME}' dari '${DB_HOST}:${DB_PORT}'..."
    if ! MYSQL_PWD="$DB_PASS" mariadb-dump \
            -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" \
            --single-transaction --quick \
            --routines --triggers --events \
            --default-character-set=utf8mb4 \
            "$DB_NAME" > "${PAYLOAD}/database.sql" 2> "${WORK}/dump.err"; then
        log "ERROR: Gagal dump database MariaDB:"
        sed 's/^/    /' "${WORK}/dump.err" | tee -a "$LOG_FILE" >&2
        exit 1
    fi
    # mariadb-dump menulis penanda ini di akhir dump yang utuh
    if ! tail -c 1024 "${PAYLOAD}/database.sql" | grep -q "Dump completed"; then
        log "ERROR: Dump database tidak lengkap (penanda akhir tidak ditemukan)."
        exit 1
    fi
    log "Dump database berhasil ($(stat -c %s "${PAYLOAD}/database.sql") bytes)."
else
    log "PERINGATAN: Konfigurasi DB belum lengkap. Dump database dilewati."
fi

# 2. Berkas aplikasi (opsional)
if [ -d "$APP_SOURCE_DIR" ] && [ -n "$(ls -A "$APP_SOURCE_DIR" 2>/dev/null)" ]; then
    log "Menyalin berkas aplikasi dari ${APP_SOURCE_DIR}..."
    cp -a "$APP_SOURCE_DIR" "${PAYLOAD}/app_data"
fi

if [ -z "$(ls -A "$PAYLOAD")" ]; then
    log "ERROR: Tidak ada data yang dibackup (DB belum dikonfigurasi dan tidak ada berkas aplikasi)."
    exit 1
fi

# 3. Tentukan penerima enkripsi (public key age)
#    Disarankan: isi 'age_recipient' di dashboard dan simpan private key OFFLINE.
RECIPIENT="$AGE_RECIPIENT"
if [ -z "$RECIPIENT" ] && [ -s "$AGE_PUB_FILE" ]; then
    RECIPIENT="$(tr -d '\r\n' < "$AGE_PUB_FILE")"
fi
if [ -z "$RECIPIENT" ]; then
    if [ ! -s "$AGE_KEY_FILE" ]; then
        age-keygen -o "$AGE_KEY_FILE" 2>/dev/null
        log "PERINGATAN KEAMANAN: keypair age dibuat otomatis. Private key ada di ${AGE_KEY_FILE}"
        log "  -> SALIN ke tempat aman lalu HAPUS dari server ini. Tanpa itu backup tidak bisa dipulihkan,"
        log "     dan jika ia tetap di sini, siapa pun yang membobol server dapat membuka backup."
    fi
    age-keygen -y "$AGE_KEY_FILE" > "$AGE_PUB_FILE"
    RECIPIENT="$(tr -d '\r\n' < "$AGE_PUB_FILE")"
fi
if ! [[ "$RECIPIENT" =~ ^age1[a-z0-9]{58}$ ]]; then
    log "ERROR: Public key age tidak valid."
    exit 1
fi

# 4. Kompres + enkripsi langsung lewat pipe (tidak ada arsip plaintext di disk), lalu rename atomik
log "Mengompres dan mengenkripsi berkas backup..."
tar -czf - -C "$PAYLOAD" . | age -r "$RECIPIENT" -o "$PARTIAL"
[ -s "$PARTIAL" ] || { log "ERROR: Arsip terenkripsi kosong."; exit 1; }
mv -f "$PARTIAL" "$OUT_PATH"
PARTIAL=""
rm -rf "$WORK"; WORK=""
log "Arsip terenkripsi dibuat: ${OUT_PATH} ($(stat -c %s "$OUT_PATH") bytes)"

# 5. Unggah ke Central Gateway (presigned URL, tanpa master key COS)
UPLOAD_OK=1
log "Mengirim file backup ke Central Gateway..."
if python3 "${SCRIPT_DIR}/cos.py" upload "$OUT_PATH" 2>&1 | tee -a "$LOG_FILE"; then
    log "Unggah ke Object Storage via Central Gateway BERHASIL."
else
    log "ERROR: Gagal mengunggah backup ke Central Gateway (arsip lokal tetap tersimpan)."
    UPLOAD_OK=0
fi

# 6. Rotasi arsip lokal
log "Membersihkan arsip lokal berusia >${RETENTION_DAYS} hari..."
find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.age' -mtime "+${RETENTION_DAYS}" -delete

if [ "$UPLOAD_OK" -eq 1 ]; then
    log "=== PROSES BACKUP SELESAI ==="
    exit 0
fi
log "=== BACKUP SELESAI DENGAN KEGAGALAN UNGGAH ==="
exit 2
