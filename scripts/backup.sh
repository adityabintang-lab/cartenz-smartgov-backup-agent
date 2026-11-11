#!/usr/bin/env bash
# Backup SmartGOV: dump MariaDB (+ berkas aplikasi) -> tar.gz -> enkripsi age -> unggah via Central Gateway.
# Exit code: 0 sukses | 1 backup gagal | 2 unggah gagal | 3 rotasi COS gagal | 75 backup lain berjalan
set -Eeuo pipefail
umask 077

CONFIG_FILE="${CONFIG_FILE:-/app/data/agent_config.json}"
BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
LOG_DIR="${LOG_DIR:-/app/logs}"
SECRETS_DIR="${SECRETS_DIR:-/app/secrets}"
APP_SOURCE_ROOT="${APP_SOURCE_ROOT:-/backup_sources}"
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
                     ("db_name", ""), ("age_recipient", ""), ("database_max_files", 7),
                     ("application_max_files", 2), ("application_source_path", "")):
    print(str(c.get(key, default)).replace("\n", " ").replace("\r", " "))
PY
)
DB_HOST="${CFG[0]:-}"; DB_PORT="${CFG[1]:-3306}"; DB_USER="${CFG[2]:-}"; DB_PASS="${CFG[3]:-}"
DB_NAME="${CFG[4]:-}"; AGE_RECIPIENT="${CFG[5]:-}"
DATABASE_MAX_FILES="${CFG[6]:-7}"; APPLICATION_MAX_FILES="${CFG[7]:-2}"
APP_SOURCE_SUBPATH="${CFG[8]:-}"
APP_SOURCE_DIR=""

[[ "$DB_PORT" =~ ^[0-9]+$ ]] || DB_PORT=3306
for limit_name in DATABASE_MAX_FILES APPLICATION_MAX_FILES; do
    limit_value="${!limit_name}"
    default_limit=7
    if [ "$limit_name" = "APPLICATION_MAX_FILES" ]; then default_limit=2; fi
    if [[ ! "$limit_value" =~ ^[0-9]+$ ]]; then
        printf -v "$limit_name" '%s' "$default_limit"
        continue
    fi
    limit_value=$((10#$limit_value))
    if (( limit_value < 1 || limit_value > 3650 )); then limit_value="$default_limit"; fi
    printf -v "$limit_name" '%s' "$limit_value"
done

TIMESTAMP="$(date +%Y%m%d_%H%M%S)_$$"

# Bersihkan sisa staging/partial dari proses yang pernah terhenti paksa
find "$STAGING_ROOT" -mindepth 1 -maxdepth 1 -mtime +1 -exec rm -rf {} + 2>/dev/null || true
find "$BACKUP_DIR" -type f -name '*.partial' -mtime +1 -delete 2>/dev/null || true

WORK="$(mktemp -d -p "$STAGING_ROOT")"
DB_PAYLOAD="${WORK}/database"
APP_PAYLOAD="${WORK}/application"
mkdir -p "$DB_PAYLOAD" "$APP_PAYLOAD"
DB_READY=0
APP_READY=0
BACKUP_FAILED=0
UPLOAD_FAILED=0
ROTATION_FAILED=0

# 1. Dump seluruh database dalam satu arsip khusus database.
if [ -n "$DB_HOST" ] && [ -n "$DB_USER" ] && [ -n "$DB_NAME" ]; then
    log "Dump seluruh database dari '${DB_HOST}:${DB_PORT}'..."
    if ! MYSQL_PWD="$DB_PASS" mariadb-dump \
            -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" \
            --single-transaction --quick \
            --routines --triggers --events \
            --default-character-set=utf8mb4 \
            --all-databases > "${DB_PAYLOAD}/database.sql" 2> "${WORK}/dump.err"; then
        log "ERROR: Gagal dump database MariaDB:"
        sed 's/^/    /' "${WORK}/dump.err" | tee -a "$LOG_FILE" >&2
        BACKUP_FAILED=1
    elif ! tail -c 1024 "${DB_PAYLOAD}/database.sql" | grep -q "Dump completed"; then
        log "ERROR: Dump database tidak lengkap (penanda akhir tidak ditemukan)."
        BACKUP_FAILED=1
    else
        DB_READY=1
        log "Dump seluruh database berhasil ($(stat -c %s "${DB_PAYLOAD}/database.sql") bytes)."
    fi
else
    log "PERINGATAN: Konfigurasi koneksi DB belum lengkap. Dump database dilewati."
fi

# 2. Berkas aplikasi (opsional), dipilih relatif terhadap root yang di-mount read-only.
if [ -n "$APP_SOURCE_SUBPATH" ]; then
    if [[ "$APP_SOURCE_SUBPATH" = /* || "$APP_SOURCE_SUBPATH" == *\\* ||
          "$APP_SOURCE_SUBPATH" =~ (^|/)\.\.?(/|$) ]]; then
        log "ERROR: Path aplikasi harus relatif dan tidak boleh keluar dari root yang di-mount."
        BACKUP_FAILED=1
    else
        APP_SOURCE_ROOT_REAL="$(realpath -e "$APP_SOURCE_ROOT" 2>/dev/null || true)"
        APP_SOURCE_DIR="$(realpath -e "${APP_SOURCE_ROOT}/${APP_SOURCE_SUBPATH}" 2>/dev/null || true)"
        if [ -z "$APP_SOURCE_ROOT_REAL" ]; then
            log "ERROR: Root sumber aplikasi ${APP_SOURCE_ROOT} tidak tersedia di container."
            BACKUP_FAILED=1
        elif [ -z "$APP_SOURCE_DIR" ] || [ ! -d "$APP_SOURCE_DIR" ]; then
            log "ERROR: Subfolder aplikasi '${APP_SOURCE_SUBPATH}' tidak ditemukan di root sumber."
            APP_SOURCE_DIR=""
            BACKUP_FAILED=1
        elif [[ "$APP_SOURCE_DIR" != "$APP_SOURCE_ROOT_REAL/"* ]]; then
            log "ERROR: Path aplikasi mengarah ke luar root sumber yang di-mount."
            APP_SOURCE_DIR=""
            BACKUP_FAILED=1
        elif [ -n "$(ls -A "$APP_SOURCE_DIR" 2>/dev/null)" ]; then
            log "Menyalin berkas aplikasi dari ${APP_SOURCE_DIR}..."
            if cp -a "$APP_SOURCE_DIR" "${APP_PAYLOAD}/app_data"; then
                APP_READY=1
            else
                log "ERROR: Gagal menyalin berkas aplikasi."
                BACKUP_FAILED=1
            fi
        else
            log "ERROR: Subfolder aplikasi '${APP_SOURCE_SUBPATH}' kosong atau tidak dapat dibaca."
            BACKUP_FAILED=1
        fi
    fi
else
    log "Berkas aplikasi belum dikonfigurasi; backup aplikasi dilewati."
fi

if [ "$DB_READY" -ne 1 ] && [ "$APP_READY" -ne 1 ]; then
    log "ERROR: Tidak ada data yang dibackup (DB belum dikonfigurasi dan tidak ada berkas aplikasi)."
    exit 1
fi

# 3. Tentukan penerima enkripsi (public key age), sama untuk kedua jenis arsip.
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

# 4. Buat arsip terpisah, simpan lokal, lalu rotasi file tertua berdasarkan jumlah.
prune_local() {
    local category_dir="$1" maximum="$2" excess oldest
    local -a archives=()
    mapfile -t archives < <(find "$category_dir" -maxdepth 1 -type f -name '*.age' -printf '%T@ %p\n' | sort -n | cut -d' ' -f2-)
    excess=$((${#archives[@]} - maximum))
    for ((i = 0; i < excess; i++)); do
        oldest="${archives[$i]}"
        log "Menghapus arsip lokal tertua: ${oldest}"
        if ! rm -f -- "$oldest"; then
            log "ERROR: Gagal menghapus arsip lokal ${oldest}."
            return 1
        fi
    done
}

create_archive() {
    local category="$1" payload="$2" maximum="$3"
    local category_dir="${BACKUP_DIR}/${category}"
    local output="${category_dir}/smartgov_${category}_${TIMESTAMP}.tar.gz.age"
    local partial="${output}.partial"
    if ! mkdir -p "$category_dir"; then
        log "ERROR: Gagal membuat direktori arsip ${category_dir}."
        BACKUP_FAILED=1
        return 1
    fi
    PARTIAL="$partial"
    log "Mengompres dan mengenkripsi arsip ${category}..."
    if ! tar -czf - -C "$payload" . | age -r "$RECIPIENT" -o "$partial"; then
        log "ERROR: Gagal membuat arsip ${category}."
        rm -f -- "$partial"
        PARTIAL=""
        BACKUP_FAILED=1
        return 1
    fi
    if [ ! -s "$partial" ]; then
        log "ERROR: Arsip ${category} terenkripsi kosong."
        rm -f -- "$partial"
        PARTIAL=""
        BACKUP_FAILED=1
        return 1
    fi
    if ! mv -f "$partial" "$output"; then
        log "ERROR: Gagal menyimpan arsip ${category} ke ${output}."
        BACKUP_FAILED=1
        return 1
    fi
    PARTIAL=""
    log "Arsip ${category} dibuat: ${output} ($(stat -c %s "$output") bytes)"
    case "$category" in
        database) DATABASE_ARCHIVE="$output" ;;
        application) APPLICATION_ARCHIVE="$output" ;;
    esac
    if ! prune_local "$category_dir" "$maximum"; then
        log "ERROR: Rotasi arsip lokal ${category} gagal."
        BACKUP_FAILED=1
        return 1
    fi
}

DATABASE_ARCHIVE=""
APPLICATION_ARCHIVE=""
if [ "$DB_READY" -eq 1 ]; then
    create_archive database "$DB_PAYLOAD" "$DATABASE_MAX_FILES" || BACKUP_FAILED=1
fi
if [ "$APP_READY" -eq 1 ]; then
    create_archive application "$APP_PAYLOAD" "$APPLICATION_MAX_FILES" || BACKUP_FAILED=1
fi

rm -rf "$WORK"
WORK=""
if [ -z "$DATABASE_ARCHIVE" ] && [ -z "$APPLICATION_ARCHIVE" ]; then
    log "ERROR: Tidak ada arsip yang berhasil dibuat."
    exit 1
fi

# 5. Unggah tiap jenis ke folder COS-nya sendiri, lalu hapus objek COS tertua.
upload_archive() {
    local category="$1" archive="$2" maximum="$3"
    log "Mengirim arsip ${category} ke Central Gateway..."
    if ! python3 "${SCRIPT_DIR}/cos.py" upload "$category" "$archive" 2>&1 | tee -a "$LOG_FILE"; then
        log "ERROR: Gagal mengunggah arsip ${category} (arsip lokal tetap tersimpan)."
        UPLOAD_FAILED=1
        return
    fi
    log "Unggah arsip ${category} BERHASIL."
    if python3 "${SCRIPT_DIR}/cos.py" rotate "$category" "$maximum" 2>&1 | tee -a "$LOG_FILE"; then
        log "Rotasi objek COS ${category} selesai (maksimum ${maximum} file)."
    else
        log "ERROR: Arsip ${category} terunggah, tetapi rotasi objek COS gagal."
        ROTATION_FAILED=1
    fi
}

if [ -n "$DATABASE_ARCHIVE" ]; then
    upload_archive database "$DATABASE_ARCHIVE" "$DATABASE_MAX_FILES"
fi
if [ -n "$APPLICATION_ARCHIVE" ]; then
    upload_archive application "$APPLICATION_ARCHIVE" "$APPLICATION_MAX_FILES"
fi

if [ "$BACKUP_FAILED" -eq 1 ]; then
    log "=== PROSES BACKUP SELESAI DENGAN KESALAHAN ==="
    exit 1
fi
if [ "$UPLOAD_FAILED" -eq 1 ]; then
    log "=== BACKUP LOKAL BERHASIL, UNGGAH GAGAL ==="
    exit 2
fi
if [ "$ROTATION_FAILED" -eq 1 ]; then
    log "=== BACKUP BERHASIL DIUNGGAH, ROTASI COS GAGAL ==="
    exit 3
fi
log "=== PROSES BACKUP SELESAI ==="
exit 0
