#!/usr/bin/env bash
set -euo pipefail
umask 077

# Bind mount yang dibuat Docker dimiliki root; container berjalan sebagai uid 1000.
# Gagal cepat dengan pesan yang jelas, bukan error samar saat setup disimpan.
for d in /app/data /app/backups /app/logs /app/secrets; do
    if [ ! -w "$d" ]; then
        echo "[FATAL] $d tidak dapat ditulis oleh uid $(id -u)." >&2
        echo "        Jalankan di host: sudo chown -R 1000:1000 ./agent_data ./backups ./logs ./secrets" >&2
        exit 1
    fi
done

# Penjadwal backup berjalan di dalam ui_agent.py (satu proses, status & lock terpusat).
# JANGAN menaikkan --workers: scheduler/lock bersifat per-proses.
exec uvicorn ui_agent:app --host 0.0.0.0 --port 25200 --workers 1 --no-server-header
