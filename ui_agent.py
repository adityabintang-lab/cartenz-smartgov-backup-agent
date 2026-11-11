"""SmartGOV Backup Agent: dashboard web, API, dan penjadwal backup."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# Konfigurasi path (bisa dioverride lewat env, mis. untuk pengujian)
# --------------------------------------------------------------------------- #
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/app/data/agent_config.json"))
STATE_FILE = CONFIG_FILE.with_name("backup_state.json")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/backups"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/app/secrets"))
BACKUP_SCRIPT = os.environ.get("BACKUP_SCRIPT", "/app/scripts/backup.sh")
LOG_FILE = LOG_DIR / "backup.log"
BACKUP_TIMEOUT = int(os.environ.get("BACKUP_TIMEOUT", "21600"))  # 6 jam
SCHEDULER_DELAY = int(os.environ.get("SCHEDULER_DELAY", "20"))
MIN_INTERVAL, MAX_INTERVAL = 60, 604800

# Token sekali-pakai untuk setup awal: ditampilkan di log container, bukan di jaringan.
SETUP_TOKEN = os.environ.get("SETUP_TOKEN") or secrets.token_urlsafe(24)

_config_lock = threading.Lock()
_state_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Penyimpanan config/state (atomik, permission 0600)
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"[WARN] Gagal membaca {path}: {exc}", flush=True)
        return {}


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config() -> Dict[str, Any]:
    return _read_json(CONFIG_FILE)


def save_config(data: Dict[str, Any]) -> None:
    _write_json_atomic(CONFIG_FILE, data)


def load_state() -> Dict[str, Any]:
    return _read_json(STATE_FILE)


def is_configured(cfg: Dict[str, Any]) -> bool:
    return bool((cfg.get("admin_password_hash") or cfg.get("admin_password")) and cfg.get("central_url"))


# --------------------------------------------------------------------------- #
# Password hashing (scrypt) & autentikasi
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, bytes.fromhex(dk_hex))
    except (ValueError, TypeError):
        return False


def _check_admin_password(cfg: Dict[str, Any], password: str) -> bool:
    stored = cfg.get("admin_password_hash")
    if stored:
        return verify_password(password, stored)
    legacy = cfg.get("admin_password")  # config versi lama (plaintext) -> migrasi ke hash saat login
    if legacy and hmac.compare_digest(password.encode(), str(legacy).encode()):
        with _config_lock:
            fresh = load_config()
            fresh["admin_password_hash"] = hash_password(password)
            fresh.pop("admin_password", None)
            save_config(fresh)
        return True
    return False


MAX_FAILS, FAIL_WINDOW = 5, 300
_failures: Dict[str, List[float]] = {}
_fail_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _lockout_check(key: str) -> None:
    now = time.time()
    with _fail_lock:
        recent = [t for t in _failures.get(key, []) if now - t < FAIL_WINDOW]
        _failures[key] = recent
        if len(recent) >= MAX_FAILS:
            retry = int(FAIL_WINDOW - (now - recent[0])) + 1
            raise HTTPException(429, "Terlalu banyak percobaan gagal. Coba lagi beberapa menit lagi.",
                                headers={"Retry-After": str(retry)})


def _lockout_fail(key: str) -> None:
    with _fail_lock:
        _failures.setdefault(key, []).append(time.time())


def _lockout_clear(key: str) -> None:
    with _fail_lock:
        _failures.pop(key, None)


_basic = HTTPBasic(auto_error=False)
_UNAUTH = {"WWW-Authenticate": 'Basic realm="SmartGOV Backup Agent", charset="UTF-8"'}


def require_admin(request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(_basic)) -> str:
    cfg = load_config()
    if not is_configured(cfg):
        raise HTTPException(503, "Agent belum dikonfigurasi.")
    key = "login:" + _client_ip(request)
    _lockout_check(key)
    if credentials is None:  # permintaan awal browser: belum dihitung sebagai percobaan gagal
        raise HTTPException(401, "Autentikasi diperlukan.", headers=_UNAUTH)
    # Bandingkan sebagai bytes (compare_digest pada str non-ASCII melempar TypeError -> HTTP 500)
    user_ok = hmac.compare_digest(credentials.username.encode(), str(cfg.get("admin_user", "admin")).encode())
    pass_ok = _check_admin_password(cfg, credentials.password)
    if not (user_ok and pass_ok):
        _lockout_fail(key)
        raise HTTPException(401, "Kredensial tidak valid.", headers=_UNAUTH)
    _lockout_clear(key)
    return credentials.username


def csrf_guard(request: Request) -> None:
    """Header kustom memaksa preflight CORS, sehingga situs lain tidak bisa memicu aksi
    memakai kredensial Basic yang tersimpan di browser (CSRF)."""
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        raise HTTPException(403, "Permintaan ditolak (header X-Requested-With wajib).")


# --------------------------------------------------------------------------- #
# Validasi input
# --------------------------------------------------------------------------- #
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_ASCII_PRINTABLE = re.compile(r"^[\x20-\x7e]+$")


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    admin_user: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    admin_password: Optional[str] = Field(default=None, min_length=12, max_length=128,
                                          pattern=r"^[\x20-\x7e]+$")
    db_host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_user: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    db_pass: Optional[str] = Field(default=None, max_length=256)
    db_name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_$]+$")
    central_url: str = Field(min_length=1, max_length=300)
    agent_token: Optional[str] = Field(default=None, max_length=512)
    age_recipient: Optional[str] = Field(default=None, pattern=r"^age1[a-z0-9]{58}$")
    schedule_mode: Literal["interval", "daily", "monthly"] = "interval"
    interval_seconds: int = Field(default=86400, ge=MIN_INTERVAL, le=MAX_INTERVAL)
    schedule_time: str = Field(default="02:00", min_length=5, max_length=5)
    monthly_day: int = Field(default=1, ge=1, le=31)
    database_max_files: int = Field(default=7, ge=1, le=3650)
    application_max_files: int = Field(default=2, ge=1, le=3650)

    @field_validator("admin_password", "db_pass", "agent_token", "age_recipient", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        return None if v == "" else v

    @field_validator("db_pass", "agent_token")
    @classmethod
    def _no_control_chars(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and _CTRL.search(v):
            raise ValueError("tidak boleh mengandung karakter kontrol/baris baru")
        return v

    @field_validator("central_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("harus berupa URL http:// atau https:// yang valid")
        if parts.username or parts.password:
            raise ValueError("tidak boleh memuat user/password di dalam URL")
        return v.rstrip("/")

    @field_validator("schedule_time")
    @classmethod
    def _valid_schedule_time(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%H:%M")
        except ValueError as exc:
            raise ValueError("gunakan format jam HH:MM (00:00–23:59)") from exc
        return v


FIELD_LABELS = {
    "setup_token": "Token setup", "admin_user": "Username admin", "admin_password": "Password admin",
    "db_host": "DB host", "db_port": "DB port", "db_user": "DB user", "db_pass": "DB password",
    "db_name": "Database untuk uji koneksi", "central_url": "URL server pusat", "agent_token": "Agent API token",
    "age_recipient": "Public key age", "interval_seconds": "Interval backup",
    "schedule_time": "Waktu backup", "monthly_day": "Tanggal backup bulanan",
    "database_max_files": "Maksimum arsip database",
    "application_max_files": "Maksimum arsip aplikasi",
}
PATTERN_HINTS = {
    "admin_user": "hanya huruf, angka, titik, garis bawah, dan strip",
    "admin_password": "hanya karakter ASCII yang dapat dicetak (tanpa huruf beraksen/emoji)",
    "db_host": "nama host atau IP tidak valid",
    "db_user": "hanya huruf, angka, dan . _ @ -",
    "db_name": "hanya huruf, angka, _ dan $",
    "age_recipient": "harus public key age (diawali 'age1', 62 karakter)",
}


def _friendly_errors(errors: List[Dict[str, Any]]) -> str:
    lines = []
    for e in errors:
        loc = [p for p in e.get("loc", ()) if p != "body"]
        field = str(loc[-1]) if loc else ""
        label = FIELD_LABELS.get(field, field or "Permintaan")
        kind, ctx = e.get("type", ""), e.get("ctx") or {}
        if kind == "string_too_short":
            msg = f"minimal {ctx.get('min_length')} karakter"
        elif kind == "string_too_long":
            msg = f"maksimal {ctx.get('max_length')} karakter"
        elif kind == "string_pattern_mismatch":
            msg = PATTERN_HINTS.get(field, "format tidak valid")
        elif kind == "greater_than_equal":
            msg = f"minimal {ctx.get('ge')}"
        elif kind == "less_than_equal":
            msg = f"maksimal {ctx.get('le')}"
        elif kind == "missing":
            msg = "wajib diisi"
        elif kind == "extra_forbidden":
            msg = "field tidak dikenal"
        elif kind in ("int_parsing", "int_type"):
            msg = "harus berupa angka bulat"
        elif kind == "value_error":
            msg = str(e.get("msg", "")).removeprefix("Value error, ")
        else:
            msg = e.get("msg", "tidak valid")
        lines.append(f"{label}: {msg}")
    return "\n".join(lines) or "Data tidak valid."


def build_config(existing: Dict[str, Any], data: SettingsIn) -> Dict[str, Any]:
    cfg = dict(existing)
    cfg.pop("admin_password", None)
    cfg.update(
        admin_user=data.admin_user, db_host=data.db_host, db_port=data.db_port, db_user=data.db_user,
        db_name=data.db_name, central_url=data.central_url,
        schedule_mode=data.schedule_mode, interval_seconds=data.interval_seconds,
        schedule_time=data.schedule_time, monthly_day=data.monthly_day,
        database_max_files=data.database_max_files, application_max_files=data.application_max_files,
    )
    if data.admin_password:
        cfg["admin_password_hash"] = hash_password(data.admin_password)
    if data.db_pass is not None:
        cfg["db_pass"] = data.db_pass
    if data.agent_token is not None:
        cfg["agent_token"] = data.agent_token
    if data.age_recipient:
        cfg["age_recipient"] = data.age_recipient
    else:
        cfg.pop("age_recipient", None)
    return cfg


# --------------------------------------------------------------------------- #
# Backup manager & scheduler
# --------------------------------------------------------------------------- #
def _interval(cfg: Dict[str, Any]) -> int:
    try:
        return max(MIN_INTERVAL, min(MAX_INTERVAL, int(cfg.get("interval_seconds", 86400))))
    except (TypeError, ValueError):
        return 86400


def _next_calendar_run(cfg: Dict[str, Any], finished_at: float) -> float:
    last = datetime.fromtimestamp(finished_at)
    hour, minute = 2, 0
    try:
        hour, minute = (int(part) for part in str(cfg.get("schedule_time", "02:00")).split(":"))
    except (TypeError, ValueError):
        pass

    target = last.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cfg.get("schedule_mode") == "daily":
        if target.timestamp() <= finished_at:
            target += timedelta(days=1)
        return target.timestamp()

    month = last.month
    year = last.year
    for _ in range(2):
        day = min(int(cfg.get("monthly_day", 1)), monthrange(year, month)[1])
        target = last.replace(year=year, month=month, day=day, hour=hour, minute=minute,
                              second=0, microsecond=0)
        if target.timestamp() > finished_at:
            return target.timestamp()
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return target.timestamp()


def next_run_at(cfg: Dict[str, Any], state: Dict[str, Any]) -> float:
    last = state.get("last_run")
    if not last:
        if cfg.get("schedule_mode", "interval") != "interval":
            return _next_calendar_run(cfg, time.time())
        return 0.0  # belum pernah backup: jatuh tempo segera setelah scheduler aktif
    finished_at = float(last.get("finished_at", 0))
    if cfg.get("schedule_mode", "interval") == "interval":
        interval = _interval(cfg)
        # Setelah kegagalan, coba lagi lebih cepat (maks. 1 jam), bukan menunggu interval penuh.
        wait = interval if last.get("status") == "success" else min(interval, 3600)
        return finished_at + wait
    scheduled = _next_calendar_run(cfg, finished_at)
    if last.get("status") != "success":
        return min(scheduled, finished_at + 3600)
    return scheduled


class BackupManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, trigger: str) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._run, args=(trigger,), daemon=True, name="backup").start()
        return True

    def _run(self, trigger: str) -> None:
        started = time.time()
        status = "failed"
        try:
            proc = subprocess.Popen([BACKUP_SCRIPT], stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                rc = proc.wait(timeout=BACKUP_TIMEOUT)
                status = {0: "success", 2: "local_only", 3: "retention_failed", 75: "skipped"}.get(rc, "failed")
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)  # matikan juga mariadb-dump/tar/age anaknya
                proc.wait()
                status = "timeout"
        except Exception as exc:  # noqa: BLE001
            print(f"[Scheduler] Gagal menjalankan backup: {exc}", flush=True)
        finally:
            finished = time.time()
            with _state_lock:
                state = load_state()
                state["last_run"] = {"trigger": trigger, "status": status, "started_at": started,
                                     "finished_at": finished, "duration": finished - started}
                if status == "success":
                    state["last_success_at"] = finished
                try:
                    _write_json_atomic(STATE_FILE, state)
                except OSError as exc:
                    print(f"[WARN] Gagal menyimpan state: {exc}", flush=True)
            with self._lock:
                self._running = False


manager = BackupManager()


def scheduler_loop(stop: threading.Event) -> None:
    if stop.wait(SCHEDULER_DELAY):
        return
    while not stop.is_set():
        try:
            cfg = load_config()
            if is_configured(cfg) and not manager.running and time.time() >= next_run_at(cfg, load_state()):
                print("[Scheduler] Memulai backup terjadwal...", flush=True)
                manager.start("scheduler")
        except Exception as exc:  # noqa: BLE001  (scheduler tidak boleh mati)
            print(f"[Scheduler] Error: {exc}", flush=True)
        stop.wait(10)


# --------------------------------------------------------------------------- #
# Status untuk dashboard
# --------------------------------------------------------------------------- #
def _tail(path: Path, lines: int = 60, max_bytes: int = 16384) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
        return "\n".join(chunk.splitlines()[-lines:])
    except OSError:
        return ""


def collect_status() -> Dict[str, Any]:
    cfg, state = load_config(), load_state()
    files = []
    try:
        for p in BACKUP_DIR.rglob("*.age"):
            st = p.stat()
            files.append({"name": p.relative_to(BACKUP_DIR).as_posix(), "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        pass
    files.sort(key=lambda x: x["mtime"], reverse=True)
    try:
        free = shutil.disk_usage(BACKUP_DIR).free
    except OSError:
        free = None
    return {
        "running": manager.running,
        "last_run": state.get("last_run"),
        "next_run_at": None if manager.running else max(next_run_at(cfg, state), time.time()),
        "backup_count": len(files),
        "backup_total_bytes": sum(f["size"] for f in files),
        "backups": files[:10],
        "disk_free_bytes": free,
        "log_tail": _tail(LOG_FILE),
        # Private key yang disimpan di server yang sama dengan backup = enkripsi nyaris tak berguna.
        "age_private_key_on_server": (SECRETS_DIR / "age_key.txt").exists() and not cfg.get("age_recipient"),
        "central_url_insecure": str(cfg.get("central_url", "")).startswith("http://"),
    }


# --------------------------------------------------------------------------- #
# Aplikasi
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(stop,), daemon=True, name="scheduler").start()
    if not is_configured(load_config()):
        print("=" * 64, flush=True)
        print(" Agent belum dikonfigurasi. Buka dashboard lalu masukkan TOKEN SETUP:", flush=True)
        print(f"   {SETUP_TOKEN}", flush=True)
        print("=" * 64, flush=True)
    yield
    stop.set()


app = FastAPI(title="SmartGOV Backup Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": _friendly_errors(exc.errors())})


@app.get("/health")
def healthcheck():
    return {"status": "ok"}


@app.post("/api/setup", dependencies=[Depends(csrf_guard)])
def api_setup(payload: SettingsIn, request: Request, x_setup_token: str = Header(default="")):
    key = "setup:" + _client_ip(request)
    _lockout_check(key)
    with _config_lock:
        if is_configured(load_config()):
            raise HTTPException(409, "Agent sudah dikonfigurasi. Ubah lewat menu Pengaturan di dashboard.")
        if not hmac.compare_digest(x_setup_token.encode(), SETUP_TOKEN.encode()):
            _lockout_fail(key)
            raise HTTPException(403, "Token setup salah. Lihat log container untuk token yang benar.")
        missing = [FIELD_LABELS[f] + ": wajib diisi" for f in ("admin_password", "db_pass", "agent_token")
                   if getattr(payload, f) is None]
        if missing:
            raise HTTPException(422, "\n".join(missing))
        save_config(build_config({}, payload))
    _lockout_clear(key)
    return {"status": "ok"}


@app.put("/api/config", dependencies=[Depends(csrf_guard)])
def api_update_config(payload: SettingsIn, _user: str = Depends(require_admin)):
    with _config_lock:
        save_config(build_config(load_config(), payload))
    return {"status": "ok"}


@app.get("/api/status")
def api_status(_user: str = Depends(require_admin)):
    return collect_status()


@app.post("/api/backup/run", status_code=202, dependencies=[Depends(csrf_guard)])
def api_backup_run(_user: str = Depends(require_admin)):
    if not manager.start("manual"):
        raise HTTPException(409, "Backup sedang berjalan.")
    return {"status": "started"}


@app.post("/api/test-db", dependencies=[Depends(csrf_guard)])
def api_test_db(_user: str = Depends(require_admin)):
    cfg = load_config()
    cmd = ["mariadb", "-h", str(cfg.get("db_host", "")), "-P", str(cfg.get("db_port", 3306)),
           "-u", str(cfg.get("db_user", "")), "--connect-timeout=5", "-e", "SELECT 1",
           str(cfg.get("db_name", ""))]
    try:
        proc = subprocess.run(cmd, env={**os.environ, "MYSQL_PWD": str(cfg.get("db_pass", ""))},
                              capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return {"ok": False, "message": "Klien mariadb tidak tersedia di container."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Koneksi ke database timeout."}
    if proc.returncode == 0:
        return {"ok": True, "message": "Koneksi database berhasil."}
    return {"ok": False, "message": (proc.stderr.strip() or "Koneksi gagal.")[:300]}


# --------------------------------------------------------------------------- #
# Tampilan (HTML dirender server-side; JS hanya untuk aksi & polling status)
# --------------------------------------------------------------------------- #
def esc(v: Any) -> str:
    return html.escape(str(v), quote=True)


def field(fid: str, label: str, *, type: str = "text", value: Any = "", required: bool = False,
          hint: str = "", **attrs: Any) -> str:
    a: Dict[str, Any] = {"id": fid, "name": fid, "type": type}
    if value != "":
        a["value"] = value
    a.update({k.replace("_", "-"): v for k, v in attrs.items()})
    attr = " ".join(f'{k}="{esc(v)}"' for k, v in a.items())
    if required:
        attr += " required"
    hint_html = ""
    if hint:
        attr += f' aria-describedby="{fid}-h"'
        hint_html = f'<div class="hint" id="{fid}-h">{esc(hint)}</div>'
    return f'<div class="field"><label for="{fid}">{esc(label)}</label><input {attr}>{hint_html}</div>'


def settings_form(cfg: Dict[str, Any], setup: bool) -> str:
    keep = "Kosongkan jika tidak diubah"
    interval = _interval(cfg)
    if interval % 3600 == 0:
        interval_value, interval_unit = interval // 3600, "hours"
    else:
        interval_value, interval_unit = max(1, round(interval / 60)), "minutes"
    schedule_mode = cfg.get("schedule_mode", "interval")
    token = ""
    if setup:
        token = (
            "<fieldset><legend>0. Verifikasi pemilik server</legend>"
            + field("setup_token", "Token setup", type="password", required=True, autocomplete="off",
                    hint="Token dicetak di log container saat start pertama: docker logs cartenz-smartgov-backup-agent")
            + "</fieldset>"
        )
    pw_hint = "Minimal 12 karakter, ASCII." if setup else keep + ". Minimal 12 karakter, ASCII."
    confirm = field("admin_password2", "Ulangi password admin", type="password", autocomplete="new-password",
                    required=setup)
    return (
        '<form id="settings-form" method="post" action="/" autocomplete="off">' + token
        + "<fieldset><legend>1. Akun admin dashboard</legend><div class=\"row\">"
        + field("admin_user", "Username", value=cfg.get("admin_user", "admin"), required=True,
                autocomplete="username", minlength=3, maxlength=32)
        + field("admin_password", "Password", type="password", required=setup, autocomplete="new-password",
                minlength=12, maxlength=128, hint=pw_hint)
        + confirm
        + "</div></fieldset>"
        + "<fieldset><legend>2. Database MariaDB lokal</legend><div class=\"row\">"
        + field("db_host", "Host", value=cfg.get("db_host", "smartgov-mariadb"), required=True)
        + field("db_port", "Port", type="number", value=cfg.get("db_port", 3306), required=True, min=1, max=65535)
        + field("db_name", "Database untuk uji koneksi", value=cfg.get("db_name", ""), required=True,
                hint="Backup database mencakup seluruh database pada host ini.")
        + field("db_user", "User", value=cfg.get("db_user", ""), required=True, autocomplete="off")
        + field("db_pass", "Password", type="password", required=setup, autocomplete="new-password",
                hint="" if setup else keep)
        + "</div></fieldset>"
        + "<fieldset><legend>3. Server pusat</legend><div class=\"row\">"
        + field("central_url", "Central Gateway URL", type="url", value=cfg.get("central_url", ""), required=True,
                placeholder="https://pusat.example.go.id:25300", hint="Gunakan https:// agar token terenkripsi.")
        + field("agent_token", "Agent API token", type="password", required=setup, autocomplete="new-password",
                hint="" if setup else keep)
        + "</div></fieldset>"
        + "<fieldset><legend>4. Enkripsi</legend><div class=\"row\">"
        + field("age_recipient", "Public key age (disarankan)", value=cfg.get("age_recipient", ""),
                placeholder="age1...", spellcheck="false",
                hint="Buat dengan 'age-keygen' di komputer Anda dan simpan private key OFFLINE. Jika kosong, "
                     "agent membuat keypair di server ini (kurang aman).")
        + "</div></fieldset>"
        + "<fieldset><legend>5. Jadwal backup</legend>"
        + '<div class="row"><div class="field"><label for="schedule_mode">Pola jadwal</label>'
        + '<select id="schedule_mode" name="schedule_mode">'
        + f'<option value="interval" {"selected" if schedule_mode == "interval" else ""}>Setiap interval</option>'
        + f'<option value="daily" {"selected" if schedule_mode == "daily" else ""}>Harian</option>'
        + f'<option value="monthly" {"selected" if schedule_mode == "monthly" else ""}>Bulanan</option>'
        + "</select></div></div>"
        + '<div class="row" id="interval-settings">'
        + field("interval_value", "Jalankan setiap", type="number", value=interval_value,
                required=True, min=1, max=168)
        + '<div class="field"><label for="interval_unit">Satuan interval</label>'
        + f'<select id="interval_unit" name="interval_unit">'
        + f'<option value="minutes" {"selected" if interval_unit == "minutes" else ""}>Menit</option>'
        + f'<option value="hours" {"selected" if interval_unit == "hours" else ""}>Jam</option>'
        + "</select></div></div>"
        + '<div class="row" id="calendar-settings">'
        + field("schedule_time", "Waktu (lokal server)", type="time",
                value=cfg.get("schedule_time", "02:00"), required=True)
        + '<div id="monthly-schedule-field">'
        + field("monthly_day", "Tanggal dalam bulan", type="number", value=cfg.get("monthly_day", 1),
                required=True, min=1, max=31, hint="Jika tanggal tidak ada di bulan tersebut, jadwal memakai hari terakhir.")
        + "</div></div>"
        + "</fieldset>"
        + "<fieldset><legend>6. Batas arsip per agent (lokal dan COS)</legend><div class=\"row\">"
        + field("database_max_files", "Maksimum arsip database", type="number",
                value=cfg.get("database_max_files", 7), required=True, min=1, max=3650,
                hint="Arsip lokal dirotasi setelah dibuat; arsip COS setelah berhasil diunggah.")
        + field("application_max_files", "Maksimum arsip aplikasi", type="number",
                value=cfg.get("application_max_files", 2), required=True, min=1, max=3650,
                hint="Arsip lokal dirotasi setelah dibuat; arsip COS setelah berhasil diunggah.")
        + "</div></fieldset>"
        + '<label class="check"><input type="checkbox" id="show-pw"> Tampilkan password</label>'
        + '<button type="submit" class="primary" id="save-btn">'
        + ("Simpan &amp; aktifkan agent" if setup else "Simpan pengaturan")
        + "</button></form>"
    )


CSS = """
:root{color-scheme:dark light;--bg:#0f172a;--card:#1e293b;--bd:#334155;--fg:#f8fafc;--mut:#94a3b8;--in:#0f172a;
--pri:#2563eb;--link:#60a5fa;--ok:#4ade80;--warn:#fbbf24;--err:#f87171}
@media (prefers-color-scheme:light){:root{--bg:#f1f5f9;--card:#fff;--bd:#cbd5e1;--fg:#0f172a;--mut:#475569;
--in:#fff;--link:#1d4ed8;--ok:#15803d;--warn:#b45309;--err:#b91c1c}}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.5rem;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:20px;margin-bottom:16px}
.card h2{font-size:1.05rem;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:16px}
.k{color:var(--mut);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
.v{font-weight:600;word-break:break-word}
.chip{display:inline-block;padding:1px 10px;border-radius:999px;font-size:.88rem;font-weight:600;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}
.actions{display:flex;flex-wrap:wrap;gap:8px}
.banner,.notice{border:1px solid currentColor;border-radius:8px;padding:10px 14px;margin-bottom:12px;white-space:pre-line}
fieldset{border:0;padding:0;margin:0 0 20px;min-width:0}
legend{font-weight:600;color:var(--link);padding:0;margin-bottom:8px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
label{display:block;font-size:.9rem;margin-bottom:4px}
input[type=text],input[type=password],input[type=number],input[type=url],input[type=time],select{width:100%;min-height:44px;padding:8px 12px;
font:inherit;color:inherit;background:var(--in);border:1px solid var(--bd);border-radius:8px}
.hint{color:var(--mut);font-size:.82rem;margin-top:4px}
.check{display:flex;align-items:center;gap:8px;margin:0 0 16px;min-height:44px}
.check input{width:20px;height:20px}
button{min-height:44px;padding:8px 16px;font:inherit;color:inherit;background:transparent;border:1px solid var(--bd);
border-radius:8px;cursor:pointer}
button.primary{background:var(--pri);border-color:var(--pri);color:#fff;font-weight:600}
button:disabled{opacity:.6;cursor:not-allowed}
:focus-visible{outline:3px solid var(--link);outline-offset:2px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--bd);white-space:nowrap}
pre.log{max-height:320px;overflow:auto;margin:0;padding:12px;background:var(--in);border:1px solid var(--bd);
border-radius:8px;font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
details>summary{cursor:pointer;font-weight:600;min-height:44px;display:flex;align-items:center}
"""

JS = r"""
(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const mode = document.body.dataset.mode;
let refresh = () => {};  // diisi oleh blok dashboard

async function api(method, url, body, headers) {
  const h = Object.assign({'X-Requested-With': 'XMLHttpRequest'}, headers || {});
  if (body !== undefined) h['Content-Type'] = 'application/json';
  const res = await fetch(url, {method, headers: h, credentials: 'same-origin',
                                body: body === undefined ? undefined : JSON.stringify(body)});
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const d = data && data.detail;
    const err = new Error(typeof d === 'string' ? d : 'Permintaan gagal (HTTP ' + res.status + ').');
    err.status = res.status;
    throw err;
  }
  return data;
}
function notice(kind, text) {
  const n = $('#notice');
  n.className = 'notice ' + kind;
  n.textContent = text || '';
  n.hidden = !text;
  if (text) n.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}
function busy(btn, on, label) {
  if (on) { btn.dataset.label = btn.textContent; btn.textContent = label; btn.disabled = true; }
  else { btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }
}
function mk(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString('id-ID') : '—';
function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}

/* ---------- Form pengaturan / setup ---------- */
const form = $('#settings-form');
if (form) {
  const val = (n, trim) => { const e = form.elements[n]; return !e ? '' : (trim === false ? e.value : e.value.trim()); };
  const scheduleMode = form.elements.schedule_mode;
  const intervalValue = form.elements.interval_value;
  const intervalUnit = form.elements.interval_unit;
  const scheduleTime = form.elements.schedule_time;
  const monthlyDay = form.elements.monthly_day;
  function updateScheduleFields() {
    const mode = scheduleMode.value;
    $('#interval-settings').hidden = mode !== 'interval';
    $('#calendar-settings').hidden = mode === 'interval';
    $('#monthly-schedule-field').hidden = mode !== 'monthly';
    intervalValue.required = mode === 'interval';
    scheduleTime.required = mode !== 'interval';
    monthlyDay.required = mode === 'monthly';
    intervalValue.max = intervalUnit.value === 'hours' ? '168' : '10080';
  }
  scheduleMode.addEventListener('change', updateScheduleFields);
  intervalUnit.addEventListener('change', () => {
    const value = Number(intervalValue.value) || 1;
    intervalValue.value = intervalUnit.value === 'minutes' ? String(value * 60) : String(Math.max(1, Math.round(value / 60)));
    updateScheduleFields();
  });
  updateScheduleFields();
  $('#show-pw').addEventListener('change', ev => {
    form.querySelectorAll('input[type=password],input[data-pw]').forEach(i => {
      i.type = ev.target.checked ? 'text' : 'password'; i.dataset.pw = '1';
    });
    if (!ev.target.checked) form.querySelectorAll('input[data-pw]').forEach(i => { i.type = 'password'; });
  });
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    notice('', '');
    const pw = val('admin_password', false);
    if (pw !== val('admin_password2', false)) { notice('err', 'Password admin dan konfirmasinya tidak sama.'); return; }
    const payload = {
      admin_user: val('admin_user'), db_host: val('db_host'), db_port: Number(val('db_port')),
      db_user: val('db_user'), db_name: val('db_name'), central_url: val('central_url'),
      schedule_mode: val('schedule_mode'),
      interval_seconds: Math.round(Number(val('interval_value')) * (val('interval_unit') === 'hours' ? 3600 : 60)),
      schedule_time: val('schedule_time'),
      monthly_day: Number(val('monthly_day')),
      database_max_files: Number(val('database_max_files')),
      application_max_files: Number(val('application_max_files'))
    };
    if (val('age_recipient')) payload.age_recipient = val('age_recipient');
    ['admin_password', 'db_pass', 'agent_token'].forEach(n => { const x = val(n, false); if (x) payload[n] = x; });
    const btn = $('#save-btn');
    busy(btn, true, 'Menyimpan…');
    try {
      if (mode === 'setup') {
        await api('POST', '/api/setup', payload, {'X-Setup-Token': val('setup_token', false)});
        notice('ok', 'Setup berhasil. Memuat dashboard… masuk dengan username & password admin yang baru dibuat.');
        setTimeout(() => location.reload(), 1500);
      } else {
        await api('PUT', '/api/config', payload);
        if (pw || payload.admin_user !== document.body.dataset.user) {
          notice('ok', 'Tersimpan. Kredensial admin berubah — silakan masuk ulang.');
          setTimeout(() => location.reload(), 1500);
        } else {
          notice('ok', 'Pengaturan tersimpan.');
          busy(btn, false);
          refresh();
        }
        return;
      }
    } catch (e) {
      notice('err', e.message);
      busy(btn, false);
    }
  });
}

/* ---------- Dashboard ---------- */
if (mode === 'dashboard') {
  const STATUS = {success: ['Berhasil', 'ok'], local_only: ['Arsip lokal OK, unggah gagal', 'warn'],
                  retention_failed: ['Terunggah, rotasi COS gagal', 'warn'],
                  failed: ['Gagal', 'err'], timeout: ['Melebihi batas waktu', 'err'], skipped: ['Dilewati', 'warn']};
  let timer = null;
  function render(s) {
    const chip = $('#st-last'); chip.textContent = '';
    const last = s.last_run;
    if (s.running) chip.append(mk('span', 'chip warn', 'Sedang berjalan…'));
    else if (last) { const t = STATUS[last.status] || [last.status, 'warn']; chip.append(mk('span', 'chip ' + t[1], t[0])); }
    else chip.append(mk('span', 'chip', 'Belum pernah'));
    $('#st-last-time').textContent = last
      ? fmtTime(last.finished_at) + ' · ' + Math.round(last.duration) + ' dtk · ' + (last.trigger === 'manual' ? 'manual' : 'terjadwal')
      : '—';
    $('#st-next').textContent = s.running ? '—' : (s.next_run_at - Date.now() / 1000 < 60 ? 'Segera' : fmtTime(s.next_run_at));
    $('#st-count').textContent = s.backup_count + ' arsip (' + fmtBytes(s.backup_total_bytes) + ')';
    $('#st-disk').textContent = fmtBytes(s.disk_free_bytes);
    $('#warn-key').hidden = !s.age_private_key_on_server;
    $('#warn-http').hidden = !s.central_url_insecure;
    $('#run-btn').disabled = s.running;
    const tb = $('#files'); tb.textContent = '';
    if (!s.backups.length) { const tr = mk('tr'); const td = mk('td', '', 'Belum ada arsip.'); td.colSpan = 3; tr.append(td); tb.append(tr); }
    s.backups.forEach(b => { const tr = mk('tr'); tr.append(mk('td', '', b.name), mk('td', '', fmtBytes(b.size)), mk('td', '', fmtTime(b.mtime))); tb.append(tr); });
    const log = $('#log'); const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = s.log_tail || '(belum ada log)';
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
  refresh = async function () {
    clearTimeout(timer);
    let next = 15000;
    try { const s = await api('GET', '/api/status'); render(s); if (s.running) next = 2500; }
    catch (e) { notice('err', 'Gagal memuat status: ' + e.message); }
    timer = setTimeout(refresh, next);
  };
  $('#run-btn').addEventListener('click', async ev => {
    const btn = ev.currentTarget; busy(btn, true, 'Memulai…');
    try { await api('POST', '/api/backup/run'); notice('ok', 'Backup dimulai.'); }
    catch (e) { notice(e.status === 409 ? 'warn' : 'err', e.message); }
    busy(btn, false); refresh();
  });
  $('#db-btn').addEventListener('click', async ev => {
    const btn = ev.currentTarget; busy(btn, true, 'Menguji…');
    try { const r = await api('POST', '/api/test-db'); notice(r.ok ? 'ok' : 'err', r.message); }
    catch (e) { notice('err', e.message); }
    busy(btn, false);
  });
  refresh();
}
})();
"""


def render_page(nonce: str, mode: str, user: str, inner: str) -> str:
    return (
        '<!DOCTYPE html><html lang="id"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark light"><meta name="robots" content="noindex,nofollow">'
        "<title>SmartGOV Backup Agent</title>"
        f'<style nonce="{nonce}">{CSS}</style></head>'
        f'<body data-mode="{esc(mode)}" data-user="{esc(user)}"><main class="wrap">'
        "<h1>SmartGOV Backup Agent</h1>"
        + inner
        + "<noscript><p class=\"banner err\">Dashboard ini memerlukan JavaScript.</p></noscript>"
        f'</main><script nonce="{nonce}">{JS}</script></body></html>'
    )


def setup_view() -> str:
    return (
        '<p class="sub">Setup awal — hanya bisa dilakukan sekali.</p>'
        '<div id="notice" role="alert" hidden></div>'
        '<section class="card"><h2>Setup Wizard</h2>' + settings_form({}, setup=True) + "</section>"
    )


def dashboard_view(cfg: Dict[str, Any]) -> str:
    return (
        '<p class="sub">Backup MariaDB terenkripsi dan sinkronisasi ke Object Storage.</p>'
        '<div id="notice" role="status" aria-live="polite" hidden></div>'
        '<div id="warn-key" class="banner warn" role="alert" hidden><strong>Private key enkripsi masih ada di server ini.</strong> '
        "Siapa pun yang menguasai server dapat membuka backup. Salin <code>secrets/age_key.txt</code> ke tempat aman, "
        "hapus dari server, lalu isi “Public key age” di Pengaturan.</div>"
        '<div id="warn-http" class="banner warn" role="alert" hidden>Server pusat memakai <strong>HTTP</strong> (tanpa TLS): '
        "token agent terkirim tanpa enkripsi. Gunakan https://.</div>"
        '<section class="card"><h2>Status</h2><div class="grid">'
        '<div><div class="k">Backup terakhir</div><div class="v" id="st-last">Memuat…</div>'
        '<div class="hint" id="st-last-time"></div></div>'
        '<div><div class="k">Backup berikutnya</div><div class="v" id="st-next">—</div></div>'
        '<div><div class="k">Arsip lokal</div><div class="v" id="st-count">—</div></div>'
        '<div><div class="k">Disk bebas</div><div class="v" id="st-disk">—</div></div></div>'
        '<div class="actions"><button type="button" class="primary" id="run-btn">Backup sekarang</button>'
        '<button type="button" id="db-btn">Uji koneksi database</button></div></section>'
        '<section class="card"><h2>Arsip terbaru</h2><div class="scroll"><table>'
        "<thead><tr><th>Nama</th><th>Ukuran</th><th>Waktu</th></tr></thead><tbody id=\"files\"></tbody></table></div></section>"
        '<section class="card"><h2>Log backup</h2><pre class="log" id="log" tabindex="0" aria-label="Log backup">Memuat…</pre></section>'
        '<section class="card"><details open><summary>Pengaturan Agent</summary>' + settings_form(cfg, setup=False)
        + "</details></section>"
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(_basic)):
    nonce = request.state.csp_nonce
    cfg = load_config()
    if not is_configured(cfg):
        return HTMLResponse(render_page(nonce, "setup", "", setup_view()))
    user = require_admin(request, credentials)
    return HTMLResponse(render_page(nonce, "dashboard", user, dashboard_view(cfg)))
