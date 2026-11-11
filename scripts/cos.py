#!/usr/bin/env python3
"""Unggah arsip backup ke Object Storage lewat presigned URL dari Central Gateway."""
import json
import logging
import os
import sys
import time
from urllib.parse import urlsplit, urlparse

import requests

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/app/data/agent_config.json")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))

# Konfigurasi Logging (Aktifkan mode verbose/debug dengan variabel lingkungan DEBUG=1)
DEBUG_MODE = os.environ.get("DEBUG", "0").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("agent.uploader")


class UploadError(Exception):
    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


def load_agent_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.error(f"Gagal membaca file konfigurasi {CONFIG_FILE}: {e}")
        return {}


def _valid_http_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _mask_headers(headers: dict) -> dict:
    """Menyamarkan data kredensial pada header sebelum dicetak ke log."""
    masked = dict(headers)
    if "Authorization" in masked:
        masked["Authorization"] = "Bearer ***MASKED_AGENT_TOKEN***"
    if "authorization" in masked:
        masked["authorization"] = "Bearer ***MASKED_AGENT_TOKEN***"
    return masked


def _mask_url(url: str) -> str:
    """Menyamarkan signature query string pada Presigned URL COS."""
    parsed = urlparse(url)
    if parsed.query:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?***MASKED_PRESIGNED_PARAMS***"
    return url


def _attempt(central_url: str, token: str, local_path: str, category: str) -> None:
    filename = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)

    # 1. Minta presigned upload URL (URL baru di tiap percobaan karena bisa kedaluwarsa)
    request_headers = {"Authorization": f"Bearer {token}"}
    request_params = {"filename": filename, "category": category}
    endpoint_url = f"{central_url}/api/v1/get-upload-url"

    logger.debug(f"[REQ OUT] POST {endpoint_url} | Params: {request_params}")
    logger.debug(f"[REQ OUT] Headers: {_mask_headers(request_headers)}")

    try:
        resp = requests.post(
            endpoint_url,
            params=request_params,
            headers=request_headers,
            timeout=(10, 30),
        )
    except requests.RequestException as e:
        raise UploadError(f"Tidak dapat menghubungi server pusat ({type(e).__name__})")

    logger.debug(f"[RESP IN] HTTP {resp.status_code} | Body: {resp.text[:300]}")

    if resp.status_code != 200:
        # 4xx (token salah, dsb.) tidak akan membaik dengan mengulang
        raise UploadError(
            f"Server pusat menolak permintaan URL (HTTP {resp.status_code}): {resp.text[:200]}",
            retry=resp.status_code >= 500,
        )

    try:
        upload_url = resp.json().get("upload_url")
    except ValueError:
        upload_url = None

    if not upload_url or not _valid_http_url(upload_url):
        raise UploadError("Respons server pusat tidak berisi upload_url yang valid.", retry=False)

    # 2. Stream langsung ke COS.
    put_headers = {"Content-Length": str(file_size)}
    masked_cos_url = _mask_url(upload_url)

    logger.info(f"Mengunggah berkas '{filename}' ({file_size} bytes) ke Object Storage...")
    logger.debug(f"[COS PUT OUT] URL: {masked_cos_url}")
    logger.debug(f"[COS PUT OUT] Headers: {put_headers}")

    try:
        with open(local_path, "rb") as f:
            put = requests.put(
                upload_url,
                data=f,
                headers=put_headers,
                timeout=(10, 3600),
            )
    except requests.RequestException as e:
        raise UploadError(f"Gagal mengunggah ke object storage ({type(e).__name__})")

    logger.debug(f"[COS RESP IN] HTTP {put.status_code}")

    if put.status_code not in (200, 204):
        raise UploadError(
            f"Object storage menolak unggahan (HTTP {put.status_code}).",
            retry=put.status_code >= 500,
        )


def upload_file(local_path: str, category: str) -> bool:
    cfg = load_agent_config()
    central_url = str(cfg.get("central_url", "")).rstrip("/")
    token = cfg.get("agent_token", "")

    if category not in ("database", "application", "archive"):
        logger.error("Kategori arsip tidak valid.")
        return False
    if not central_url or not token:
        logger.error("Agent belum dikonfigurasi via Web UI (central_url/agent_token kosong).")
        return False

    if not _valid_http_url(central_url):
        logger.error("central_url tidak valid (harus diawali http:// atau https://).")
        return False

    if not os.path.isfile(local_path):
        logger.error(f"Berkas tidak ditemukan: {local_path}")
        return False

    if central_url.startswith("http://"):
        logger.warning("PERINGATAN: central_url memakai HTTP tanpa TLS; token agent terkirim tanpa enkripsi!")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            logger.info(f"Memulai proses unggah (Percobaan {attempt}/{MAX_ATTEMPTS})...")
            _attempt(central_url, token, local_path, category)
            logger.info("Berhasil mengunggah berkas ke Object Storage!")
            return True
        except UploadError as e:
            logger.error(f"Percobaan {attempt}/{MAX_ATTEMPTS} gagal: {e}")
            if not e.retry or attempt == MAX_ATTEMPTS:
                return False
            backoff_time = 5 * attempt
            logger.info(f"Menunggu {backoff_time} detik sebelum mencoba lagi...")
            time.sleep(backoff_time)
    return False


def rotate_backups(category: str, max_files: int) -> bool:
    cfg = load_agent_config()
    central_url = str(cfg.get("central_url", "")).rstrip("/")
    token = cfg.get("agent_token", "")
    if category not in ("database", "application"):
        logger.error("Kategori rotasi tidak valid.")
        return False
    try:
        max_files = int(max_files)
    except (TypeError, ValueError):
        logger.error("Batas maksimum file tidak valid.")
        return False
    if not 1 <= max_files <= 3650:
        logger.error("Batas maksimum file harus antara 1 dan 3650.")
        return False
    if not central_url or not token or not _valid_http_url(central_url):
        logger.error("Agent belum memiliki URL Gateway dan token yang valid.")
        return False

    url = f"{central_url}/api/v1/rotate-backups"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.post(
            url,
            params={"category": category, "max_files": max_files},
            headers=headers,
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        logger.error(f"Gagal meminta rotasi arsip COS ({type(exc).__name__}).")
        return False
    if response.status_code != 200:
        logger.error(f"Gateway menolak rotasi arsip COS (HTTP {response.status_code}).")
        return False
    try:
        deleted = int(response.json().get("deleted", 0))
    except (ValueError, TypeError):
        logger.error("Respons rotasi arsip COS tidak valid.")
        return False
    logger.info(f"Rotasi arsip COS {category} selesai; {deleted} file lama dihapus.")
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "upload":
        if len(sys.argv) == 4:
            category, target_file = sys.argv[2], sys.argv[3]
        elif len(sys.argv) == 3:
            category, target_file = "archive", sys.argv[2]
        else:
            logger.error("Gunakan kategori dan nama berkas.")
            sys.exit(64)
        sys.exit(0 if upload_file(target_file, category) else 1)
    if len(sys.argv) == 4 and sys.argv[1] == "rotate":
        sys.exit(0 if rotate_backups(sys.argv[2], sys.argv[3]) else 1)

    sys.stderr.write("Penggunaan: cos.py upload <database|application> <berkas>\n")
    sys.stderr.write("             cos.py rotate <database|application> <maksimum-file>\n")
    sys.exit(64)
