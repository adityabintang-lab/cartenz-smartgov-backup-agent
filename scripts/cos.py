#!/usr/bin/env python3
"""Unggah arsip backup ke Object Storage lewat presigned URL dari Central Gateway."""
import json
import os
import sys
import time
from urllib.parse import urlsplit

import requests

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/app/data/agent_config.json")
MAX_ATTEMPTS = 3


class UploadError(Exception):
    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def load_agent_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _valid_http_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _attempt(central_url: str, token: str, local_path: str) -> None:
    filename = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)

    # 1. Minta presigned upload URL (URL baru di tiap percobaan karena bisa kedaluwarsa)
    try:
        resp = requests.post(
            f"{central_url}/api/v1/get-upload-url",
            params={"filename": filename},
            headers={"Authorization": f"Bearer {token}"},
            timeout=(10, 30),
        )
    except requests.RequestException as e:
        raise UploadError(f"Tidak dapat menghubungi server pusat ({type(e).__name__})")

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

    # 2. Stream langsung ke COS. Jangan log exception mentah: pesannya memuat URL bertanda tangan.
    try:
        with open(local_path, "rb") as f:
            put = requests.put(
                upload_url, data=f,
                headers={"Content-Length": str(file_size)},
                timeout=(10, 3600),
            )
    except requests.RequestException as e:
        raise UploadError(f"Gagal mengunggah ke object storage ({type(e).__name__})")
    if put.status_code not in (200, 204):
        raise UploadError(f"Object storage menolak unggahan (HTTP {put.status_code}).",
                          retry=put.status_code >= 500)


def upload_file(local_path: str) -> bool:
    cfg = load_agent_config()
    central_url = str(cfg.get("central_url", "")).rstrip("/")
    token = cfg.get("agent_token", "")

    if not central_url or not token:
        log("Agent belum dikonfigurasi via Web UI.")
        return False
    if not _valid_http_url(central_url):
        log("central_url tidak valid (harus http:// atau https://).")
        return False
    if not os.path.isfile(local_path):
        log(f"Berkas tidak ditemukan: {local_path}")
        return False
    if central_url.startswith("http://"):
        log("PERINGATAN: central_url memakai HTTP tanpa TLS; token agent terkirim tanpa enkripsi.")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _attempt(central_url, token, local_path)
            return True
        except UploadError as e:
            log(f"Percobaan {attempt}/{MAX_ATTEMPTS} gagal: {e}")
            if not e.retry or attempt == MAX_ATTEMPTS:
                return False
            time.sleep(5 * attempt)
    return False


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "upload":
        sys.exit(0 if upload_file(sys.argv[2]) else 1)
    sys.stderr.write("Penggunaan: cos.py upload <berkas>\n")
    sys.exit(64)
