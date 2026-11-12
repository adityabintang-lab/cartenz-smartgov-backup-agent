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
SINGLE_PUT_LIMIT = 4 * 1024**3
PART_URL_BATCH_SIZE = 8


class UploadError(Exception):
    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


class LimitedFileReader:
    def __init__(self, source, length: int):
        self.source = source
        self.remaining = length

    def __len__(self):
        return self.remaining

    def read(self, size: int = -1):
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.remaining
        data = self.source.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


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


def _gateway_post(
    central_url: str,
    endpoint: str,
    token: str,
    payload: dict,
    timeout=(10, 30),
) -> dict:
    try:
        response = requests.post(
            f"{central_url}{endpoint}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise UploadError(f"Tidak dapat menghubungi Gateway ({type(exc).__name__}).") from exc
    if response.status_code != 200:
        raise UploadError(
            f"Gateway menolak permintaan multipart (HTTP {response.status_code}).",
            retry=response.status_code == 429 or response.status_code >= 500,
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise UploadError("Gateway memberikan respons multipart yang tidak valid.", retry=False) from exc
    if not isinstance(result, dict):
        raise UploadError("Gateway memberikan respons multipart yang tidak valid.", retry=False)
    return result


def _get_part_urls(central_url: str, token: str, payload: dict) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _gateway_post(
                central_url,
                "/api/v1/multipart/part-urls",
                token,
                payload,
            )
        except UploadError as exc:
            if not exc.retry or attempt == MAX_ATTEMPTS:
                raise
            logger.warning("Gagal meminta URL bagian multipart; akan mencoba lagi.")
            time.sleep(5 * attempt)
    raise UploadError("Gagal meminta URL bagian multipart.")


def _parse_part_urls(result: dict, expected_numbers: list) -> dict:
    parts = result.get("parts")
    if not isinstance(parts, list):
        raise UploadError("Gateway memberikan URL bagian multipart yang tidak valid.", retry=False)
    url_by_number = {}
    for part in parts:
        if (
            not isinstance(part, dict)
            or type(part.get("part_number")) is not int
            or not _valid_http_url(part.get("upload_url"))
        ):
            raise UploadError("Gateway memberikan URL bagian multipart yang tidak valid.", retry=False)
        number = part["part_number"]
        if number in url_by_number:
            raise UploadError("Gateway memberikan nomor bagian multipart ganda.", retry=False)
        url_by_number[number] = part["upload_url"]
    if set(url_by_number) != set(expected_numbers):
        raise UploadError("Gateway tidak memberikan semua URL bagian multipart.", retry=False)
    return url_by_number


def _abort_multipart(central_url: str, token: str, payload: dict) -> None:
    try:
        _gateway_post(
            central_url,
            "/api/v1/multipart/abort",
            token,
            payload,
        )
    except UploadError as exc:
        logger.error("Tidak dapat membatalkan multipart upload di COS: %s", exc)


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

    logger.debug(f"[RESP IN] HTTP {resp.status_code}")

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


def _multipart_upload(central_url: str, token: str, local_path: str, category: str, file_size: int) -> None:
    filename = os.path.basename(local_path)
    base_payload = {"filename": filename, "category": category}
    init_payload = {**base_payload, "file_size": file_size}
    init_result = _gateway_post(
        central_url,
        "/api/v1/multipart/initiate",
        token,
        init_payload,
    )
    upload_id = init_result.get("upload_id")
    part_size = init_result.get("part_size")
    total_parts = init_result.get("total_parts")
    if not isinstance(upload_id, str) or not upload_id:
        raise UploadError("Gateway memberikan parameter multipart yang tidak valid.", retry=False)

    abort_payload = {**base_payload, "upload_id": upload_id}
    completed = False
    try:
        if (
            type(part_size) is not int
            or not 0 < part_size <= 5_000_000_000
            or type(total_parts) is not int
            or total_parts != (file_size + part_size - 1) // part_size
            or not 1 <= total_parts <= 10_000
        ):
            raise UploadError("Gateway memberikan parameter multipart yang tidak valid.", retry=False)
        try:
            initial_stat = os.stat(local_path)
            if initial_stat.st_size != file_size:
                raise UploadError("Ukuran arsip berubah sebelum unggahan multipart dimulai.", retry=False)

            uploaded_parts = []
            with open(local_path, "rb") as source:
                for batch_start in range(1, total_parts + 1, PART_URL_BATCH_SIZE):
                    part_numbers = list(
                        range(batch_start, min(batch_start + PART_URL_BATCH_SIZE, total_parts + 1))
                    )
                    url_result = _get_part_urls(
                        central_url,
                        token,
                        {
                            **abort_payload,
                            "part_numbers": part_numbers,
                        },
                    )
                    url_by_number = _parse_part_urls(url_result, part_numbers)

                    for part_number in part_numbers:
                        offset = (part_number - 1) * part_size
                        part_length = min(part_size, file_size - offset)
                        upload_url = url_by_number[part_number]
                        part_etag = None
                        for attempt in range(1, MAX_ATTEMPTS + 1):
                            source.seek(offset)
                            reader = LimitedFileReader(source, part_length)
                            try:
                                response = requests.put(
                                    upload_url,
                                    data=reader,
                                    headers={"Content-Length": str(part_length)},
                                    timeout=(10, 3600),
                                )
                            except requests.RequestException as exc:
                                if attempt == MAX_ATTEMPTS:
                                    raise UploadError(
                                        f"Gagal mengunggah bagian {part_number}/{total_parts} "
                                        f"({type(exc).__name__})."
                                    ) from exc
                                logger.warning("Unggah bagian %s gagal; akan mencoba lagi.", part_number)
                                time.sleep(5 * attempt)
                                continue

                            if response.status_code in (200, 201, 204):
                                if reader.remaining:
                                    raise UploadError(
                                        f"Bagian {part_number} tidak terkirim secara utuh.",
                                        retry=False,
                                    )
                                part_etag = response.headers.get("ETag")
                                if not part_etag:
                                    raise UploadError(
                                        f"COS tidak mengembalikan ETag untuk bagian {part_number}.",
                                        retry=False,
                                    )
                                break
                            if response.status_code == 403 and attempt < MAX_ATTEMPTS:
                                logger.warning("URL bagian %s kedaluwarsa; meminta URL pengganti.", part_number)
                                refreshed = _get_part_urls(
                                    central_url,
                                    token,
                                    {
                                        **abort_payload,
                                        "part_numbers": [part_number],
                                    },
                                )
                                upload_url = _parse_part_urls(refreshed, [part_number])[part_number]
                                continue
                            retryable = response.status_code == 429 or response.status_code >= 500
                            if not retryable or attempt == MAX_ATTEMPTS:
                                raise UploadError(
                                    f"COS menolak bagian {part_number}/{total_parts} "
                                    f"(HTTP {response.status_code}).",
                                    retry=retryable,
                                )
                            logger.warning(
                                "COS gagal menerima bagian %s (HTTP %s); akan mencoba lagi.",
                                part_number,
                                response.status_code,
                            )
                            time.sleep(5 * attempt)

                        if part_etag is None:
                            raise UploadError(f"Unggah bagian {part_number} tidak berhasil.")
                        uploaded_parts.append({"part_number": part_number, "etag": part_etag})
                        logger.info("Bagian %s/%s berhasil diunggah.", part_number, total_parts)

            current_stat = os.stat(local_path)
            if (current_stat.st_size, current_stat.st_mtime_ns) != (
                initial_stat.st_size,
                initial_stat.st_mtime_ns,
            ):
                raise UploadError("Arsip berubah saat multipart upload berlangsung.", retry=False)

            _gateway_post(
                central_url,
                "/api/v1/multipart/complete",
                token,
                {**abort_payload, "parts": uploaded_parts},
                timeout=(10, 1200),
            )
            completed = True
        except OSError as exc:
            raise UploadError(f"Gagal membaca arsip untuk multipart upload ({type(exc).__name__}).", retry=False) from exc
    finally:
        if not completed:
            _abort_multipart(central_url, token, abort_payload)


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

    try:
        file_size = os.path.getsize(local_path)
    except OSError as exc:
        logger.error("Gagal membaca ukuran berkas (%s).", type(exc).__name__)
        return False

    if file_size > SINGLE_PUT_LIMIT:
        logger.info(
            "Arsip berukuran %s byte melebihi batas PUT tunggal; memakai multipart upload.",
            file_size,
        )
        try:
            _multipart_upload(central_url, token, local_path, category, file_size)
        except UploadError as exc:
            logger.error("Multipart upload gagal: %s", exc)
            return False
        logger.info("Berhasil mengunggah berkas multipart ke Object Storage!")
        return True

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
