#!/usr/bin/env bash
# =========================================================
# SMARTGOV BACKUP AGENT - SKRIP INSTALASI / UPDATE
# Pemakaian:  sudo bash install.sh [--bind 0.0.0.0] [--dir /opt/smartgov-agent]
#   --bind  alamat bind dashboard (default 127.0.0.1; gunakan 0.0.0.0 HANYA di belakang HTTPS)
#   --dir   lokasi instalasi (default /opt/smartgov-agent)
# Aman dijalankan berulang kali: menarik kode terbaru dan membangun ulang image.
# =========================================================
set -Eeuo pipefail

APP_DIR="/opt/smartgov-agent"
APP_DIR_SET=0
REPO_URL="https://github.com/adityabintang-lab/cartenz-smartgov-backup-agent.git"
BRANCH="main"
NETWORK="smartgov-network"      # harus sama dengan network di docker-compose.yml
CONTAINER="cartenz-smartgov-backup-agent"
BIND_ADDR=""
TOTAL=7

die()  { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "[$1/${TOTAL}] $2"; }
run()  { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
set_env_value() {
    local key="$1" value="$2" env_file="${APP_DIR}/.env" tmp_file
    tmp_file="$(run mktemp "${APP_DIR}/.env.XXXXXX")"
    if run test -f "${env_file}"; then
        run awk -v key="${key}" 'index($0, key "=") != 1' "${env_file}" | run tee "${tmp_file}" >/dev/null
    fi
    printf '%s=%s\n' "${key}" "${value}" | run tee -a "${tmp_file}" >/dev/null
    run chmod 600 "${tmp_file}"
    run mv -f "${tmp_file}" "${env_file}"
}
trap 'echo "ERROR: instalasi berhenti di baris $LINENO." >&2' ERR

while [ $# -gt 0 ]; do
    case "$1" in
        --bind) [ $# -ge 2 ] || die "--bind membutuhkan nilai."; BIND_ADDR="$2"; shift 2 ;;
        --dir)  [ $# -ge 2 ] || die "--dir membutuhkan nilai.";  APP_DIR="$2"; APP_DIR_SET=1; shift 2 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) die "Argumen tidak dikenal: $1" ;;
    esac
done
if [ "$APP_DIR_SET" -eq 0 ] && [ -t 0 ]; then
    read -r -p "Direktori instalasi Agent [${APP_DIR}]: " INPUT_APP_DIR
    APP_DIR="${INPUT_APP_DIR:-$APP_DIR}"
fi
if [ -n "$BIND_ADDR" ] && ! [[ "$BIND_ADDR" =~ ^[0-9a-fA-F:.]+$ ]]; then die "Nilai --bind tidak valid: $BIND_ADDR"; fi
if [ "$(id -u)" -ne 0 ]; then command -v sudo >/dev/null 2>&1 || die "Jalankan sebagai root atau pasang sudo."; fi

echo "================================================="
echo "       MEMULAI SETUP SMARTGOV AGENT"
echo "================================================="

# ---------------------------------------------------------
step 1 "Memeriksa prasyarat (git, Docker, Docker Compose)..."
command -v git    >/dev/null 2>&1 || die "git belum terpasang."
command -v docker >/dev/null 2>&1 || die "Docker belum terpasang."
if run docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1 && run docker-compose version >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "Docker Compose tidak ditemukan!"
fi
echo "Docker Compose ditemukan: ${COMPOSE[*]}"

# ---------------------------------------------------------
step 2 "Mengambil kode aplikasi..."
if [ -d "${APP_DIR}/.git" ]; then
    echo "Repository sudah ada, menarik pembaruan..."
    if ! run git -C "${APP_DIR}" pull --ff-only; then
        echo "PERINGATAN: git pull gagal (ada perubahan lokal/konflik). Memakai kode yang sudah ada." >&2
    fi
elif [ -d "${APP_DIR}" ] && [ -n "$(run ls -A "${APP_DIR}")" ]; then
    die "Direktori ${APP_DIR} tidak kosong dan bukan repository Git."
else
    run mkdir -p "${APP_DIR}"
    run git clone --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi
[ -f "${APP_DIR}/docker-compose.yml" ] || die "docker-compose.yml tidak ditemukan di ${APP_DIR}"

# ---------------------------------------------------------
step 3 "Membuat direktori kerja..."
DIRS=("${APP_DIR}/agent_data" "${APP_DIR}/backups" "${APP_DIR}/logs" "${APP_DIR}/secrets")
run mkdir -p "${DIRS[@]}"

# ---------------------------------------------------------
step 4 "Mengatur ownership (uid 1000) dan permission (700)..."
run chown -R 1000:1000 "${DIRS[@]}"
run chmod 700 "${DIRS[@]}"   # berisi config/kredensial & arsip backup: hanya pemilik

# ---------------------------------------------------------
step 5 "Memastikan Docker network '${NETWORK}'..."
if run docker network inspect "${NETWORK}" >/dev/null 2>&1; then
    echo "Network sudah ada."
else
    run docker network create "${NETWORK}" >/dev/null
    echo "Network dibuat. Agar agent dapat menjangkau MariaDB lewat nama host, hubungkan container MariaDB:"
    echo "    docker network connect ${NETWORK} <nama-container-mariadb>"
fi

# ---------------------------------------------------------
step 6 "Menulis konfigurasi (.env)..."
if [ -n "${BIND_ADDR}" ]; then
    set_env_value AGENT_BIND_ADDR "${BIND_ADDR}"
    echo "Dashboard akan di-bind ke ${BIND_ADDR}:25200"
    [ "${BIND_ADDR}" = "127.0.0.1" ] || echo "PERINGATAN: dashboard memakai HTTP Basic; pasang HTTPS (reverse proxy) sebelum diakses lewat jaringan."
else
    echo "Memakai default: dashboard hanya di 127.0.0.1 (akses remote via SSH tunnel)."
fi

# ---------------------------------------------------------
step 7 "Membangun image dari kode terbaru dan menjalankan container..."
cd "${APP_DIR}"
# --build WAJIB: tanpa itu Compose memakai image lama (v1.0) yang sudah ada/di-pull dari registry.
run "${COMPOSE[@]}" up -d --build

echo "Menunggu agent siap..."
READY=0
for _ in $(seq 1 30); do
    if run docker exec "${CONTAINER}" curl -fsS http://127.0.0.1:25200/health >/dev/null 2>&1; then READY=1; break; fi
    sleep 2
done
if [ "${READY}" -ne 1 ]; then
    echo "---- 40 baris log terakhir ----" >&2
    run docker logs --tail 40 "${CONTAINER}" >&2 || true
    die "Agent tidak merespons dalam 60 detik. Periksa log di atas."
fi

# ---------------------------------------------------------
echo
echo "================================================="
echo "       SETUP SMARTGOV AGENT BERHASIL"
echo "================================================="
echo
echo "Lokasi aplikasi : ${APP_DIR}"
echo "Lokasi backup   : ${APP_DIR}/backups"
echo "Lokasi logs     : ${APP_DIR}/logs"
echo
run docker ps --filter "name=${CONTAINER}"

if ! run test -s "${APP_DIR}/agent_data/agent_config.json"; then
    TOKEN="$(run docker logs "${CONTAINER}" 2>&1 | awk '/TOKEN SETUP/{getline; gsub(/[[:space:]]/,""); t=$0} END{print t}')"
    ADDR="${BIND_ADDR:-127.0.0.1}"
    echo
    echo "Agent BELUM disetup. Langkah berikutnya:"
    if [ "${ADDR}" = "127.0.0.1" ]; then
        HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
        echo "  1. Dari komputer Anda:  ssh -L 25200:127.0.0.1:25200 <user>@${HOST_IP:-IP_SERVER}"
        echo "  2. Buka http://127.0.0.1:25200"
        echo "  3. Masukkan TOKEN SETUP berikut di wizard: ${TOKEN:-<lihat: docker logs ${CONTAINER}>}"
    else
        echo "  1. Buka http://${ADDR}:25200 (atau alamat HTTPS reverse proxy Anda)"
        echo "  2. Masukkan TOKEN SETUP berikut di wizard: ${TOKEN:-<lihat: docker logs ${CONTAINER}>}"
    fi
else
    echo
    echo "Agent sudah dikonfigurasi sebelumnya; pengaturan dan data dipertahankan."
fi
echo
echo "Setup selesai!"
