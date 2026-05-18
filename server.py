from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


DEFAULT_UPSTREAM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_COOLDOWN_SECONDS = 90
DEFAULT_MAX_RETRIES = 10
DEFAULT_KEY_MAX_IN_FLIGHT = 1
DEFAULT_KEY_QUEUE_WAIT_SECONDS = 30
DEFAULT_MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_DATABASE_PATH = "sub2api.db"
DEFAULT_ACCESS_LOG_HEALTH = False

MODEL_ALIASES = {
    "deepseekv4-pro": "deepseek-ai/deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-ai/deepseek-v4-pro",
    "deepseekv4": "deepseek-ai/deepseek-v4-pro",
    "deepseek-ai/deepseek-v4-pro": "deepseek-ai/deepseek-v4-pro",
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "kimik2.6": "moonshotai/kimi-k2.6",
    "kimi-k2-6": "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.6": "moonshotai/kimi-k2.6",
    "glm5.1": "z-ai/glm5.1",
    "glm-5.1": "z-ai/glm5.1",
    "z-ai/glm5.1": "z-ai/glm5.1",
    "z-ai/glm-5.1": "z-ai/glm5.1",
    "llama-3.3-70b": "meta/llama-3.3-70b-instruct",
    "llama3.3-70b": "meta/llama-3.3-70b-instruct",
    "meta/llama-3.3-70b-instruct": "meta/llama-3.3-70b-instruct",
    "nemotron-super-49b": "nvidia/llama-3.3-nemotron-super-49b-v1",
    "llama-3.3-nemotron-super-49b": "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1": "nvidia/llama-3.3-nemotron-super-49b-v1",
    "qwen3-next-80b": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen3-next": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen3-coder-480b": "qwen/qwen3-coder-480b-a35b-instruct",
    "qwen3-coder": "qwen/qwen3-coder-480b-a35b-instruct",
    "qwen/qwen3-coder-480b-a35b-instruct": "qwen/qwen3-coder-480b-a35b-instruct",
}

PUBLIC_TO_UPSTREAM_MODEL = {
    "deepseekv4-pro": "deepseek-ai/deepseek-v4-pro",
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "glm-5.1": "z-ai/glm5.1",
    "llama-3.3-70b": "meta/llama-3.3-70b-instruct",
    "nemotron-super-49b": "nvidia/llama-3.3-nemotron-super-49b-v1",
    "qwen3-next-80b": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen3-coder-480b": "qwen/qwen3-coder-480b-a35b-instruct",
}

MODEL_LIST = [
    {
        "id": public_id,
        "object": "model",
        "created": 0,
        "owned_by": "nvidia-nim",
        "root": upstream_id,
    }
    for public_id, upstream_id in PUBLIC_TO_UPSTREAM_MODEL.items()
]


class ConfigError(RuntimeError):
    pass


class UpstreamCapacityError(RuntimeError):
    pass


class RequestBodyTooLarge(ValueError):
    pass


@dataclass
class NvidiaAccountCredential:
    index: int
    email: str
    password: str
    enabled: bool = True
    note: str = ""

    def public_id(self) -> str:
        return f"acct-{self.index + 1:02d}"

    def masked_email(self) -> str:
        return mask_email(self.email)


@dataclass
class AppConfig:
    bind_host: str
    port: int
    admin_token: str
    database_path: str
    upstream_url: str
    timeout_seconds: int
    cooldown_seconds: int
    max_retries: int
    key_max_in_flight: int
    key_queue_wait_seconds: int
    max_request_body_bytes: int
    access_log_health: bool
    api_keys: List[str]
    account_credentials: List[NvidiaAccountCredential]


@dataclass
class ApiUser:
    id: int
    name: str
    token_hash: str
    quota_tokens: int
    used_tokens: int
    enabled: bool
    created_at: str
    note: str

    @property
    def remaining_tokens(self) -> int:
        if self.quota_tokens < 0:
            return -1
        return max(0, self.quota_tokens - self.used_tokens)


@dataclass
class ApiKeyState:
    index: int
    key: str
    fail_count: int = 0
    success_count: int = 0
    in_flight: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""

    def public_id(self) -> str:
        return f"nvapi-{self.index + 1:02d}"

    def is_available(self, now: float, max_in_flight: int) -> bool:
        return now >= self.cooldown_until and self.in_flight < max_in_flight


class ApiKeyPool:
    def __init__(
        self,
        keys: List[str],
        cooldown_seconds: int,
        max_in_flight: int = DEFAULT_KEY_MAX_IN_FLIGHT,
        queue_wait_seconds: int = DEFAULT_KEY_QUEUE_WAIT_SECONDS,
    ) -> None:
        if not keys:
            raise ConfigError("NVIDIA_API_KEYS is empty.")
        self._keys = [ApiKeyState(index=i, key=key) for i, key in enumerate(keys)]
        self._cooldown_seconds = cooldown_seconds
        self._max_in_flight = max(1, max_in_flight)
        self._queue_wait_seconds = max(0, queue_wait_seconds)
        self._cursor = 0
        self._condition = threading.Condition(threading.Lock())

    @property
    def size(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> List[str]:
        with self._condition:
            return [state.key for state in self._keys]

    def pick(self) -> ApiKeyState:
        deadline = time.time() + self._queue_wait_seconds
        with self._condition:
            while True:
                now = time.time()
                total = len(self._keys)

                for offset in range(total):
                    index = (self._cursor + offset) % total
                    state = self._keys[index]
                    if state.is_available(now, self._max_in_flight):
                        state.in_flight += 1
                        self._cursor = (index + 1) % total
                        return state

                remaining = deadline - now
                if remaining <= 0:
                    raise UpstreamCapacityError(
                        "All NVIDIA API keys are busy or cooling down. Try again shortly."
                    )

                wait_seconds = 0.25
                cooldown_waits = [
                    state.cooldown_until - now
                    for state in self._keys
                    if state.in_flight < self._max_in_flight and state.cooldown_until > now
                ]
                if cooldown_waits:
                    wait_seconds = min(wait_seconds, max(0.05, min(cooldown_waits)))
                self._condition.wait(timeout=min(wait_seconds, remaining))

    def _release(self, state: ApiKeyState) -> None:
        state.in_flight = max(0, state.in_flight - 1)
        self._condition.notify_all()

    def mark_success(self, state: ApiKeyState) -> None:
        with self._condition:
            state.success_count += 1
            state.last_error = ""
            self._release(state)

    def mark_failure(self, state: ApiKeyState, message: str, retryable: bool) -> None:
        with self._condition:
            state.fail_count += 1
            state.last_error = message[:300]
            if retryable:
                state.cooldown_until = time.time() + self._cooldown_seconds
            self._release(state)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._condition:
            now = time.time()
            return [
                {
                    "id": state.public_id(),
                    "success_count": state.success_count,
                    "fail_count": state.fail_count,
                    "in_flight": state.in_flight,
                    "max_in_flight": self._max_in_flight,
                    "cooldown_seconds_remaining": max(0, int(state.cooldown_until - now)),
                    "last_error": state.last_error,
                }
                for state in self._keys
            ]

    def add_keys(self, keys: List[str]) -> int:
        cleaned = [key.strip() for key in keys if key.strip()]
        if not cleaned:
            return 0
        with self._condition:
            existing = {state.key for state in self._keys}
            added = 0
            for key in cleaned:
                if key in existing:
                    continue
                self._keys.append(ApiKeyState(index=len(self._keys), key=key))
                existing.add(key)
                added += 1
            if added:
                self._condition.notify_all()
            return added


class UsageStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    quota_tokens INTEGER NOT NULL DEFAULT -1,
                    used_tokens INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    model TEXT NOT NULL,
                    upstream_model TEXT NOT NULL,
                    upstream_key_id TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    status_code INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY(user_id) REFERENCES api_users(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_user_created ON request_logs(user_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_model_created ON request_logs(model, created_at)")

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def generate_client_token() -> str:
        return "sk-" + secrets.token_urlsafe(32)

    def create_user(self, name: str, quota_tokens: int, note: str = "", token: Optional[str] = None) -> Tuple[ApiUser, str]:
        name = name.strip()
        if not name:
            raise ValueError("User name is required.")
        raw_token = token or self.generate_client_token()
        token_hash = self.hash_token(raw_token)
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO api_users (name, token_hash, quota_tokens, note)
                VALUES (?, ?, ?, ?)
                """,
                (name, token_hash, quota_tokens, note.strip()),
            )
            user = self.get_user_by_id(cursor.lastrowid, conn=conn)
            if user is None:
                raise RuntimeError("Failed to create user.")
            return user, raw_token

    def get_user_by_id(self, user_id: int, conn: Optional[sqlite3.Connection] = None) -> Optional[ApiUser]:
        close = conn is None
        conn = conn or self.connect()
        try:
            row = conn.execute("SELECT * FROM api_users WHERE id = ?", (user_id,)).fetchone()
            return row_to_user(row) if row else None
        finally:
            if close:
                conn.close()

    def get_user_by_token(self, token: str) -> Optional[ApiUser]:
        token_hash = self.hash_token(token)
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM api_users WHERE token_hash = ?", (token_hash,)).fetchone()
            return row_to_user(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.*,
                    COUNT(r.id) AS request_count,
                    COALESCE(AVG(CASE WHEN r.success = 1 THEN r.latency_ms END), 0) AS avg_latency_ms,
                    COALESCE(SUM(CASE WHEN r.success = 1 THEN 1 ELSE 0 END), 0) AS success_count,
                    COALESCE(SUM(CASE WHEN r.success = 0 THEN 1 ELSE 0 END), 0) AS error_count
                FROM api_users u
                LEFT JOIN request_logs r ON r.user_id = u.id
                GROUP BY u.id
                ORDER BY u.id DESC
                """
            ).fetchall()
        return [user_row_to_public_dict(row) for row in rows]

    def update_user(self, user_id: int, fields: Dict[str, Any]) -> Optional[ApiUser]:
        allowed = {"name", "quota_tokens", "enabled", "note"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_user_by_id(user_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [int(value) if key in {"quota_tokens", "enabled"} else str(value) for key, value in updates.items()]
        values.append(user_id)
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE api_users SET {assignments} WHERE id = ?", values)
            return self.get_user_by_id(user_id, conn=conn)

    def delete_user(self, user_id: int) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM api_users WHERE id = ?", (user_id,))

    def has_users(self) -> bool:
        with self._lock, self.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM api_users").fetchone()[0]
        return count > 0

    def record_request(
        self,
        user_id: Optional[int],
        model: str,
        upstream_model: str,
        upstream_key_id: str,
        usage: Dict[str, int],
        latency_ms: int,
        status_code: int,
        success: bool,
        error: str = "",
    ) -> None:
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0)
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO request_logs (
                    user_id, model, upstream_model, upstream_key_id, prompt_tokens, completion_tokens,
                    total_tokens, latency_ms, status_code, success, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    model,
                    upstream_model,
                    upstream_key_id,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    latency_ms,
                    int(status_code),
                    1 if success else 0,
                    error[:1000],
                ),
            )
            if user_id is not None and success and total_tokens > 0:
                conn.execute("UPDATE api_users SET used_tokens = used_tokens + ? WHERE id = ?", (total_tokens, user_id))

    def summary(self) -> Dict[str, Any]:
        with self._lock, self.connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS request_count,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(AVG(CASE WHEN success = 1 THEN latency_ms END), 0) AS avg_latency_ms,
                    COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS success_count,
                    COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS error_count
                FROM request_logs
                """
            ).fetchone()
            active_users = conn.execute("SELECT COUNT(*) FROM api_users WHERE enabled = 1").fetchone()[0]
            model_rows = conn.execute(
                """
                SELECT model, COUNT(*) AS request_count, COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(AVG(CASE WHEN success = 1 THEN latency_ms END), 0) AS avg_latency_ms
                FROM request_logs
                GROUP BY model
                ORDER BY request_count DESC
                """
            ).fetchall()
            recent_rows = conn.execute(
                """
                SELECT r.*, u.name AS user_name
                FROM request_logs r
                LEFT JOIN api_users u ON u.id = r.user_id
                ORDER BY r.id DESC
                LIMIT 30
                """
            ).fetchall()
            user_totals = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN quota_tokens >= 0 THEN quota_tokens ELSE 0 END), 0) AS quota_tokens,
                    COALESCE(SUM(CASE WHEN quota_tokens >= 0 THEN max(quota_tokens - used_tokens, 0) ELSE 0 END), 0) AS balance_tokens
                FROM api_users
                """
            ).fetchone()
        return {
            "request_count": int(totals["request_count"]),
            "total_tokens": int(totals["total_tokens"]),
            "prompt_tokens": int(totals["prompt_tokens"]),
            "completion_tokens": int(totals["completion_tokens"]),
            "avg_latency_ms": round(float(totals["avg_latency_ms"] or 0), 1),
            "success_count": int(totals["success_count"]),
            "error_count": int(totals["error_count"]),
            "active_users": int(active_users),
            "quota_tokens": int(user_totals["quota_tokens"] or 0),
            "balance_tokens": int(user_totals["balance_tokens"] or 0),
            "models": [dict(row) for row in model_rows],
            "recent_requests": [dict(row) for row in recent_rows],
        }


def row_to_user(row: sqlite3.Row) -> ApiUser:
    return ApiUser(
        id=int(row["id"]),
        name=str(row["name"]),
        token_hash=str(row["token_hash"]),
        quota_tokens=int(row["quota_tokens"]),
        used_tokens=int(row["used_tokens"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        note=str(row["note"]),
    )


def user_to_public_dict(user: ApiUser) -> Dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "quota_tokens": user.quota_tokens,
        "used_tokens": user.used_tokens,
        "remaining_tokens": user.remaining_tokens,
        "enabled": user.enabled,
        "created_at": user.created_at,
        "note": user.note,
    }


def user_row_to_public_dict(row: sqlite3.Row) -> Dict[str, Any]:
    user = row_to_user(row)
    data = user_to_public_dict(user)
    data.update(
        {
            "request_count": int(row["request_count"]),
            "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 1),
            "success_count": int(row["success_count"]),
            "error_count": int(row["error_count"]),
        }
    )
    return data


def split_csv_env(value: str) -> List[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    else:
        masked_local = local[:2] + "***" + local[-1:]
    return f"{masked_local}@{domain}"


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    try:
        return parse_bool(value, default=default)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def parse_account_line(line: str) -> Tuple[str, str, bool, str]:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) < 2:
        raise ValueError("Account entries must contain at least email and password separated by '|'.")
    email = parts[0]
    password = parts[1]
    enabled = parse_bool(parts[2], default=True) if len(parts) >= 3 and parts[2] else True
    note = parts[3] if len(parts) >= 4 else ""
    return email, password, enabled, note


def account_from_dict(index: int, item: Dict[str, Any]) -> NvidiaAccountCredential:
    email = str(item.get("email", "")).strip()
    password = str(item.get("password", "")).strip()
    enabled = parse_bool(item.get("enabled"), default=True)
    note = str(item.get("note", "")).strip()
    return NvidiaAccountCredential(index=index, email=email, password=password, enabled=enabled, note=note)


def validate_account(account: NvidiaAccountCredential) -> None:
    if not account.email:
        raise ConfigError(f"NVIDIA account {account.public_id()} email is empty.")
    if "@" not in account.email:
        raise ConfigError(f"NVIDIA account {account.public_id()} email is invalid.")
    if not account.password:
        raise ConfigError(f"NVIDIA account {account.public_id()} password is empty.")


def parse_account_pool_json(raw: str) -> List[NvidiaAccountCredential]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"NVIDIA account pool JSON is invalid: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("accounts", [])
    if not isinstance(payload, list):
        raise ConfigError("NVIDIA account pool JSON must be a list or an object with an accounts list.")
    accounts = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ConfigError(f"NVIDIA account item #{index + 1} must be an object.")
        account = account_from_dict(index, item)
        validate_account(account)
        accounts.append(account)
    return accounts


def parse_account_pool_lines(raw: str) -> List[NvidiaAccountCredential]:
    accounts = []
    rows = []
    for line in raw.replace(",", "\n").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            rows.append(stripped)
    for index, row in enumerate(rows):
        email, password, enabled, note = parse_account_line(row)
        account = NvidiaAccountCredential(index=index, email=email, password=password, enabled=enabled, note=note)
        validate_account(account)
        accounts.append(account)
    return accounts


def parse_account_pool(raw: str) -> List[NvidiaAccountCredential]:
    stripped = raw.lstrip("\ufeff").strip()
    if not stripped:
        return []
    if stripped.startswith("[") or stripped.startswith("{"):
        return parse_account_pool_json(stripped)
    return parse_account_pool_lines(stripped)


def load_account_pool(env: Optional[Dict[str, str]] = None) -> List[NvidiaAccountCredential]:
    source = env if env is not None else os.environ
    path = source.get("NVIDIA_ACCOUNT_POOL_FILE", "").strip()
    raw = source.get("NVIDIA_ACCOUNT_POOL", "").strip()
    if raw:
        return parse_account_pool(raw)
    if path:
        if not os.path.exists(path):
            raise ConfigError(f"NVIDIA_ACCOUNT_POOL_FILE does not exist: {path}")
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = handle.read()
    return parse_account_pool(raw)


def account_pool_snapshot(accounts: List[NvidiaAccountCredential]) -> List[Dict[str, Any]]:
    return [
        {
            "id": account.public_id(),
            "email": account.masked_email(),
            "enabled": account.enabled,
            "note": account.note,
        }
        for account in accounts
    ]


def read_dotenv_values(path: str = ".env") -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            values[key] = value
    return values


def load_dotenv(path: str = ".env") -> None:
    for key, value in read_dotenv_values(path).items():
        os.environ.setdefault(key, value)


def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required.")
    return value


def load_config() -> AppConfig:
    load_dotenv()
    api_keys = split_csv_env(get_required_env("NVIDIA_API_KEYS"))
    account_credentials = load_account_pool()
    admin_token = os.environ.get("ADMIN_TOKEN", "").strip() or os.environ.get("SUB2API_ACCESS_TOKEN", "").strip()
    if not admin_token:
        raise ConfigError("ADMIN_TOKEN is required.")

    return AppConfig(
        bind_host=os.environ.get("BIND_HOST", DEFAULT_HOST),
        port=int(os.environ.get("PORT", str(DEFAULT_PORT))),
        admin_token=admin_token,
        database_path=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        upstream_url=os.environ.get("UPSTREAM_URL", DEFAULT_UPSTREAM_URL),
        timeout_seconds=int(os.environ.get("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
        cooldown_seconds=int(os.environ.get("KEY_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS))),
        max_retries=int(os.environ.get("MAX_RETRIES", str(DEFAULT_MAX_RETRIES))),
        key_max_in_flight=int(os.environ.get("KEY_MAX_IN_FLIGHT", str(DEFAULT_KEY_MAX_IN_FLIGHT))),
        key_queue_wait_seconds=int(os.environ.get("KEY_QUEUE_WAIT_SECONDS", str(DEFAULT_KEY_QUEUE_WAIT_SECONDS))),
        max_request_body_bytes=int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(DEFAULT_MAX_REQUEST_BODY_BYTES))),
        access_log_health=parse_bool_env("ACCESS_LOG_HEALTH", DEFAULT_ACCESS_LOG_HEALTH),
        api_keys=api_keys,
        account_credentials=account_credentials,
    )


def response_json(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def response_html(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


def error_payload(message: str, error_type: str = "proxy_error", code: Optional[str] = None) -> Dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": code,
        }
    }


def normalize_model(model: str) -> str:
    normalized = MODEL_ALIASES.get(model.strip().lower())
    if not normalized:
        supported = ", ".join(sorted(PUBLIC_TO_UPSTREAM_MODEL.keys()))
        raise ValueError(f"Unsupported model '{model}'. Supported models: {supported}")
    return normalized


def upstream_to_public_model(upstream_model: str) -> str:
    for public_model, upstream in PUBLIC_TO_UPSTREAM_MODEL.items():
        if upstream == upstream_model:
            return public_model
    return upstream_model


def extract_bearer_token(headers: Any) -> str:
    authorization = headers.get("Authorization", "")
    if not authorization.lower().startswith("bearer "):
        return ""
    return authorization[7:].strip()


def admin_token_from_request(handler: BaseHTTPRequestHandler) -> str:
    token = extract_bearer_token(handler.headers)
    if token:
        return token
    parsed = urlparse(handler.path)
    return parse_qs(parsed.query).get("token", [""])[0]


def read_json_body(handler: BaseHTTPRequestHandler, max_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES) -> Dict[str, Any]:
    try:
        content_length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("Invalid Content-Length header.") from exc
    if content_length <= 0:
        return {}
    if content_length > max_bytes:
        raise RequestBodyTooLarge(f"Request body is too large. Max allowed size is {max_bytes} bytes.")
    body = handler.rfile.read(content_length)
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def estimate_prompt_tokens(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    total_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            total_chars += len(json.dumps(content, ensure_ascii=False))
    return max(1, total_chars // 4) if total_chars else 0


def estimate_stream_usage(payload: Dict[str, Any], completion_text: str) -> Dict[str, int]:
    prompt_tokens = estimate_prompt_tokens(payload.get("messages"))
    completion_tokens = max(1, len(completion_text) // 4) if completion_text else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def extract_usage(payload: Dict[str, Any]) -> Dict[str, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else {}
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def messages_from_responses_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    response_input = payload.get("input")
    if isinstance(response_input, str):
        messages.append({"role": "user", "content": response_input})
        return messages

    if isinstance(response_input, list):
        for item in response_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "user"))
            content = responses_content_to_text(item.get("content", item.get("text", "")))
            if content:
                messages.append({"role": role, "content": content})
        if messages:
            return messages

    if "messages" in payload and isinstance(payload["messages"], list):
        return payload["messages"]
    return messages


def responses_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("input_text") or part.get("content")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(piece for piece in pieces if piece)
    if content is None:
        return ""
    return str(content)


def chat_payload_from_responses(payload: Dict[str, Any], upstream_model: str) -> Dict[str, Any]:
    chat_payload: Dict[str, Any] = {
        "model": upstream_model,
        "messages": messages_from_responses_payload(payload),
        "stream": False,
    }
    for key in ("max_tokens", "max_completion_tokens", "temperature", "top_p", "stop", "seed", "tools", "tool_choice"):
        if key in payload:
            chat_payload[key] = payload[key]
    if "max_output_tokens" in payload and "max_tokens" not in chat_payload:
        chat_payload["max_tokens"] = payload["max_output_tokens"]
    return chat_payload


def responses_payload_from_chat(chat_payload: Dict[str, Any], public_model: str, upstream_model: str) -> Dict[str, Any]:
    created = int(chat_payload.get("created", time.time()))
    choices = chat_payload.get("choices") if isinstance(chat_payload, dict) else []
    content = ""
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    usage = extract_usage(chat_payload)
    return {
        "id": chat_payload.get("id", f"resp_{secrets.token_urlsafe(18)}"),
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": public_model,
        "output": [
            {
                "id": f"msg_{secrets.token_urlsafe(12)}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }
        ],
        "output_text": content,
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
        },
        "upstream_model": upstream_model,
    }


def build_responses_sse_events(response_payload: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    response_id = str(response_payload.get("id", f"resp_{secrets.token_urlsafe(18)}"))
    output = response_payload.get("output")
    output_item: Dict[str, Any] = {}
    if isinstance(output, list) and output and isinstance(output[0], dict):
        output_item = output[0]
    item_id = str(output_item.get("id", f"msg_{secrets.token_urlsafe(12)}"))
    text = response_payload.get("output_text", "")
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)

    sequence = 0

    def event_payload(event_type: str, **items: Any) -> Dict[str, Any]:
        nonlocal sequence
        sequence += 1
        payload = {"type": event_type, "sequence_number": sequence}
        payload.update(items)
        return payload

    created_response = dict(response_payload)
    created_response["id"] = response_id
    created_response["status"] = "in_progress"
    completed_response = dict(response_payload)
    completed_response["id"] = response_id
    completed_response["status"] = "completed"

    events: List[Tuple[str, Dict[str, Any]]] = [
        ("response.created", event_payload("response.created", response=created_response)),
        ("response.in_progress", event_payload("response.in_progress", response=created_response)),
    ]
    if text:
        events.append(
            (
                "response.output_text.delta",
                event_payload(
                    "response.output_text.delta",
                    response_id=response_id,
                    item_id=item_id,
                    output_index=0,
                    content_index=0,
                    delta=text,
                ),
            )
        )
        events.append(
            (
                "response.output_text.done",
                event_payload(
                    "response.output_text.done",
                    response_id=response_id,
                    item_id=item_id,
                    output_index=0,
                    content_index=0,
                    text=text,
                ),
            )
        )
    events.append(("response.completed", event_payload("response.completed", response=completed_response)))
    return events


def write_sse_event(handler: BaseHTTPRequestHandler, event: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    handler.wfile.write(f"event: {event}\n".encode("utf-8"))
    handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))


def parse_upstream_error(exc: HTTPError) -> Tuple[int, str]:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    if raw:
        try:
            payload = json.loads(raw)
            message = payload.get("error", {}).get("message") or payload.get("message") or raw
            return exc.code, str(message)
        except json.JSONDecodeError:
            return exc.code, raw[:1000]
    return exc.code, exc.reason or f"HTTP {exc.code}"


def is_retryable_status(status_code: int) -> bool:
    return status_code in (408, 409, 425, 429, 500, 502, 503, 504)


def nvidia_status_url(upstream_url: str, request_id: str) -> str:
    parsed = urlparse(upstream_url)
    return f"{parsed.scheme}://{parsed.netloc}/v1/status/{request_id}"


def extract_nvidia_request_id(payload: Dict[str, Any], headers: Any) -> str:
    for key in ("requestId", "request_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("NVCF-REQID", "NVCF-REQ-ID", "X-Request-Id", "Request-Id"):
        value = headers.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    location = headers.get("Location")
    if isinstance(location, str) and location.strip():
        return location.rstrip("/").split("/")[-1].strip()

    return ""


def unwrap_nvidia_poll_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("response", "result", "output"):
        value = payload.get(key)
        if isinstance(value, dict) and ("choices" in value or "error" in value):
            return value
        if isinstance(value, str) and value.strip():
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict) and ("choices" in decoded or "error" in decoded):
                return decoded
    return payload


class NvidiaProxy:
    def __init__(self, config: AppConfig, store: UsageStore) -> None:
        self.config = config
        self.store = store
        self.pool = ApiKeyPool(
            config.api_keys,
            config.cooldown_seconds,
            config.key_max_in_flight,
            config.key_queue_wait_seconds,
        )

    def build_upstream_request(self, payload: Dict[str, Any], api_key: str, stream: bool) -> Request:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "sub2api-nvidia-dashboard/1.0",
        }
        return Request(self.config.upstream_url, data=data, headers=headers, method="POST")

    def build_status_request(self, request_id: str, api_key: str) -> Request:
        return Request(
            nvidia_status_url(self.config.upstream_url, request_id),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "sub2api-nvidia-dashboard/1.0",
            },
            method="GET",
        )

    def poll_accepted_request(self, request_id: str, api_key: str, deadline: float) -> Tuple[int, Dict[str, Any]]:
        last_error = f"NVIDIA request {request_id} is still pending."
        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            timeout = min(15, remaining)
            try:
                with urlopen(self.build_status_request(request_id, api_key), timeout=timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    status_code = int(response.status)

                try:
                    payload = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    return HTTPStatus.BAD_GATEWAY, error_payload(
                        f"NVIDIA status polling returned non-JSON for request {request_id}.",
                        code="upstream_non_json",
                    )

                if status_code == HTTPStatus.OK:
                    return HTTPStatus.OK, unwrap_nvidia_poll_payload(payload)

                if status_code == HTTPStatus.ACCEPTED:
                    last_error = f"NVIDIA request {request_id} is still pending."
                    time.sleep(min(2, max(0.1, deadline - time.time())))
                    continue

                return status_code, payload
            except HTTPError as exc:
                status_code, message = parse_upstream_error(exc)
                if status_code == HTTPStatus.ACCEPTED:
                    time.sleep(min(2, max(0.1, deadline - time.time())))
                    continue
                return status_code, error_payload(f"NVIDIA status polling failed: {message}", code=str(status_code))
            except (URLError, TimeoutError, OSError) as exc:
                last_error = f"NVIDIA status polling connection failed for request {request_id}: {exc}"
                time.sleep(min(2, max(0.1, deadline - time.time())))

        return HTTPStatus.GATEWAY_TIMEOUT, error_payload(last_error, code="upstream_poll_timeout")

    def request_json(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str]:
        last_error = "No upstream attempt was made."
        attempts = min(max(1, self.config.max_retries), self.pool.size)

        for _ in range(attempts):
            try:
                state = self.pool.pick()
            except UpstreamCapacityError as exc:
                return HTTPStatus.SERVICE_UNAVAILABLE, error_payload(str(exc), code="upstream_busy"), ""
            request = self.build_upstream_request(payload, state.key, stream=False)
            try:
                deadline = time.time() + self.config.timeout_seconds
                with urlopen(request, timeout=self.config.timeout_seconds) as upstream:
                    raw = upstream.read().decode("utf-8", errors="replace")
                    status_code = int(upstream.status)
                    headers = upstream.headers
                try:
                    upstream_payload = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError as exc:
                    message = f"Upstream returned non-JSON response via {state.public_id()}: {exc}"
                    self.pool.mark_failure(state, message, retryable=True)
                    last_error = message
                    continue

                if status_code == HTTPStatus.ACCEPTED:
                    request_id = extract_nvidia_request_id(upstream_payload, headers)
                    if not request_id:
                        message = f"Upstream returned HTTP 202 via {state.public_id()} without a requestId."
                        self.pool.mark_failure(state, message, retryable=True)
                        last_error = message
                        continue
                    poll_status, poll_payload = self.poll_accepted_request(request_id, state.key, deadline)
                    if int(poll_status) < 400 and "error" not in poll_payload:
                        self.pool.mark_success(state)
                        return poll_status, poll_payload, state.public_id()
                    else:
                        retryable = is_retryable_status(int(poll_status))
                        self.pool.mark_failure(state, f"HTTP {poll_status}: {poll_payload}", retryable=retryable)
                        last_error = f"Upstream {state.public_id()} polling failed with HTTP {poll_status}."
                        if not retryable:
                            return poll_status, poll_payload, state.public_id()
                        continue

                self.pool.mark_success(state)
                return status_code, upstream_payload, state.public_id()
            except HTTPError as exc:
                status_code, message = parse_upstream_error(exc)
                retryable = is_retryable_status(status_code)
                self.pool.mark_failure(state, f"HTTP {status_code}: {message}", retryable=retryable)
                last_error = f"Upstream {state.public_id()} failed with HTTP {status_code}: {message}"
                if not retryable:
                    return status_code, error_payload(last_error, code=str(status_code)), state.public_id()
            except (URLError, TimeoutError, OSError) as exc:
                message = str(exc)
                self.pool.mark_failure(state, message, retryable=True)
                last_error = f"Upstream {state.public_id()} connection failed: {message}"

        return HTTPStatus.BAD_GATEWAY, error_payload(last_error, code="upstream_exhausted"), ""

    def stream_response(
        self,
        handler: BaseHTTPRequestHandler,
        user: ApiUser,
        public_model: str,
        upstream_model: str,
        payload: Dict[str, Any],
        started_at: float,
    ) -> None:
        attempts = min(max(1, self.config.max_retries), self.pool.size)
        last_error = "No upstream attempt was made."

        for attempt in range(attempts):
            try:
                state = self.pool.pick()
            except UpstreamCapacityError as exc:
                latency_ms = int((time.time() - started_at) * 1000)
                last_error = str(exc)
                self.store.record_request(user.id, public_model, upstream_model, "", {}, latency_ms, HTTPStatus.SERVICE_UNAVAILABLE, False, last_error)
                response_json(handler, HTTPStatus.SERVICE_UNAVAILABLE, error_payload(last_error, code="upstream_busy"))
                return
            request = self.build_upstream_request(payload, state.key, stream=True)
            completion_parts: List[str] = []
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as upstream:
                    handler.send_response(HTTPStatus.OK)
                    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    handler.send_header("Cache-Control", "no-cache")
                    handler.send_header("Connection", "keep-alive")
                    handler.send_header("X-Sub2Api-Key-Id", state.public_id())
                    handler.end_headers()

                    buffer = ""
                    while True:
                        chunk = upstream.read(8192)
                        if not chunk:
                            break
                        buffer += chunk.decode("utf-8", errors="ignore")
                        buffer, complete = consume_sse_buffer(buffer)
                        if complete:
                            completion_parts.append(extract_text_from_sse_chunk(complete.encode("utf-8")))
                        handler.wfile.write(chunk)
                        handler.wfile.flush()

                    if buffer:
                        completion_parts.append(extract_text_from_sse_chunk(buffer.encode("utf-8")))

                self.pool.mark_success(state)
                latency_ms = int((time.time() - started_at) * 1000)
                usage = estimate_stream_usage(payload, "".join(completion_parts))
                self.store.record_request(user.id, public_model, upstream_model, state.public_id(), usage, latency_ms, 200, True)
                return
            except HTTPError as exc:
                status_code, message = parse_upstream_error(exc)
                retryable = is_retryable_status(status_code)
                self.pool.mark_failure(state, f"HTTP {status_code}: {message}", retryable=retryable)
                last_error = f"Upstream {state.public_id()} failed with HTTP {status_code}: {message}"
                if not retryable or attempt == attempts - 1:
                    latency_ms = int((time.time() - started_at) * 1000)
                    self.store.record_request(
                        user.id, public_model, upstream_model, state.public_id(), {}, latency_ms, status_code, False, last_error
                    )
                    response_json(handler, status_code if not retryable else HTTPStatus.BAD_GATEWAY, error_payload(last_error))
                    return
            except (URLError, TimeoutError, OSError) as exc:
                message = str(exc)
                self.pool.mark_failure(state, message, retryable=True)
                last_error = f"Upstream {state.public_id()} connection failed: {message}"

        latency_ms = int((time.time() - started_at) * 1000)
        self.store.record_request(user.id, public_model, upstream_model, "", {}, latency_ms, HTTPStatus.BAD_GATEWAY, False, last_error)
        response_json(handler, HTTPStatus.BAD_GATEWAY, error_payload(last_error, code="upstream_exhausted"))


def extract_text_from_sse_chunk(chunk: bytes) -> str:
    text = chunk.decode("utf-8", errors="ignore")
    pieces: List[str] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = payload.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        if isinstance(content, str):
            pieces.append(content)
    return "".join(pieces)


def consume_sse_buffer(buffer: str) -> Tuple[str, str]:
    if "\n" not in buffer:
        return buffer, ""
    lines = buffer.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        remainder = lines.pop()
    else:
        remainder = ""
    return remainder, "".join(lines)


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "sub2api-nvidia/2.0"
    proxy: NvidiaProxy
    store: UsageStore
    config: AppConfig

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self.redirect("/dashboard")
            return

        if path == "/health":
            response_json(
                self,
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "upstream_url": self.config.upstream_url,
                    "key_count": self.proxy.pool.size,
                    "account_count": len(self.config.account_credentials),
                    "enabled_account_count": sum(1 for account in self.config.account_credentials if account.enabled),
                    "key_max_in_flight": self.config.key_max_in_flight,
                    "key_queue_wait_seconds": self.config.key_queue_wait_seconds,
                    "max_request_body_bytes": self.config.max_request_body_bytes,
                    "models": [model["id"] for model in MODEL_LIST],
                },
            )
            return

        if path == "/dashboard":
            if not self.authorized_admin():
                return
            response_html(self, HTTPStatus.OK, render_dashboard(self.store, self.proxy.pool, self.config))
            return

        if path == "/v1/models":
            user = self.authorized_user()
            if user is None:
                return
            response_json(self, HTTPStatus.OK, {"object": "list", "data": MODEL_LIST})
            return

        if path == "/v1/me":
            user = self.authorized_user()
            if user is None:
                return
            response_json(self, HTTPStatus.OK, {"user": user_to_public_dict(user)})
            return

        if path == "/api/admin/summary":
            if not self.authorized_admin():
                return
            response_json(self, HTTPStatus.OK, self.admin_summary())
            return

        if path == "/api/admin/users":
            if not self.authorized_admin():
                return
            response_json(self, HTTPStatus.OK, {"data": self.store.list_users()})
            return

        if path == "/api/admin/pool":
            if not self.authorized_admin():
                return
            response_json(self, HTTPStatus.OK, {"data": self.proxy.pool.snapshot()})
            return

        if path == "/api/admin/accounts":
            if not self.authorized_admin():
                return
            response_json(self, HTTPStatus.OK, {"data": account_pool_snapshot(self.config.account_credentials)})
            return

        response_json(self, HTTPStatus.NOT_FOUND, error_payload("Not found.", "invalid_request_error", "not_found"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/v1/chat/completions":
            self.handle_chat_completions()
            return

        if path == "/v1/responses":
            self.handle_responses()
            return

        if path == "/api/admin/users":
            self.handle_create_user()
            return

        if path == "/api/admin/pool/reload":
            self.handle_reload_pool()
            return

        response_json(self, HTTPStatus.NOT_FOUND, error_payload("Not found.", "invalid_request_error", "not_found"))

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/admin/users/"):
            self.handle_update_user(path)
            return
        response_json(self, HTTPStatus.NOT_FOUND, error_payload("Not found.", "invalid_request_error", "not_found"))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/admin/users/"):
            self.handle_delete_user(path)
            return
        response_json(self, HTTPStatus.NOT_FOUND, error_payload("Not found.", "invalid_request_error", "not_found"))

    def handle_chat_completions(self) -> None:
        user = self.authorized_user()
        if user is None:
            return

        if not user.enabled:
            response_json(self, HTTPStatus.FORBIDDEN, error_payload("User token is disabled.", "authentication_error", "token_disabled"))
            return

        if user.quota_tokens >= 0 and user.used_tokens >= user.quota_tokens:
            response_json(self, HTTPStatus.PAYMENT_REQUIRED, error_payload("User token quota exhausted.", "quota_error", "quota_exhausted"))
            return

        started_at = time.time()
        public_model = ""
        upstream_model = ""
        try:
            payload = read_json_body(self, self.config.max_request_body_bytes)
            upstream_model = normalize_model(str(payload.get("model", "")))
            public_model = upstream_to_public_model(upstream_model)
            payload["model"] = upstream_model
        except RequestBodyTooLarge as exc:
            response_json(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error_payload(str(exc), "invalid_request_error", "payload_too_large"))
            return
        except ValueError as exc:
            response_json(self, HTTPStatus.BAD_REQUEST, error_payload(str(exc), "invalid_request_error", "bad_request"))
            return

        stream = bool(payload.get("stream"))
        if stream:
            self.proxy.stream_response(self, user, public_model, upstream_model, payload, started_at)
            return

        status, upstream_payload, key_id = self.proxy.request_json(payload)
        latency_ms = int((time.time() - started_at) * 1000)
        success = int(status) < 400 and "error" not in upstream_payload
        usage = extract_usage(upstream_payload)
        error = ""
        if not success:
            error = str(upstream_payload.get("error", {}).get("message", "upstream_error"))
        self.store.record_request(user.id, public_model, upstream_model, key_id, usage, latency_ms, int(status), success, error)

        body = json.dumps(upstream_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if key_id:
            self.send_header("X-Sub2Api-Key-Id", key_id)
        self.end_headers()
        self.wfile.write(body)

    def handle_responses(self) -> None:
        user = self.authorized_user()
        if user is None:
            return

        if not user.enabled:
            response_json(self, HTTPStatus.FORBIDDEN, error_payload("User token is disabled.", "authentication_error", "token_disabled"))
            return

        if user.quota_tokens >= 0 and user.used_tokens >= user.quota_tokens:
            response_json(self, HTTPStatus.PAYMENT_REQUIRED, error_payload("User token quota exhausted.", "quota_error", "quota_exhausted"))
            return

        started_at = time.time()
        try:
            payload = read_json_body(self, self.config.max_request_body_bytes)
            stream_requested = bool(payload.get("stream"))
            if stream_requested:
                # Some Sub2API OpenAI-compatible routing paths translate
                # non-streaming chat requests into Responses requests that
                # still carry stream=true. We fetch NVIDIA non-streaming and
                # synthesize a minimal Responses SSE stream below.
                payload["stream"] = False
            upstream_model = normalize_model(str(payload.get("model", "")))
            public_model = upstream_to_public_model(upstream_model)
            chat_payload = chat_payload_from_responses(payload, upstream_model)
        except RequestBodyTooLarge as exc:
            response_json(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error_payload(str(exc), "invalid_request_error", "payload_too_large"))
            return
        except ValueError as exc:
            response_json(self, HTTPStatus.BAD_REQUEST, error_payload(str(exc), "invalid_request_error", "bad_request"))
            return

        status, upstream_payload, key_id = self.proxy.request_json(chat_payload)
        latency_ms = int((time.time() - started_at) * 1000)
        success = int(status) < 400 and "error" not in upstream_payload
        usage = extract_usage(upstream_payload)
        error = ""
        response_payload = upstream_payload
        if success:
            response_payload = responses_payload_from_chat(upstream_payload, public_model, upstream_model)
        else:
            error = str(upstream_payload.get("error", {}).get("message", "upstream_error"))
        self.store.record_request(user.id, public_model, upstream_model, key_id, usage, latency_ms, int(status), success, error)

        if stream_requested and success:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            if key_id:
                self.send_header("X-Sub2Api-Key-Id", key_id)
            self.end_headers()
            for event, event_payload in build_responses_sse_events(response_payload):
                write_sse_event(self, event, event_payload)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if key_id:
            self.send_header("X-Sub2Api-Key-Id", key_id)
        self.end_headers()
        self.wfile.write(body)

    def handle_create_user(self) -> None:
        if not self.authorized_admin():
            return
        try:
            payload = read_json_body(self, self.config.max_request_body_bytes)
            quota_tokens = int(payload.get("quota_tokens", -1))
            user, token = self.store.create_user(
                name=str(payload.get("name", "")).strip(),
                quota_tokens=quota_tokens,
                note=str(payload.get("note", "")).strip(),
                token=str(payload["token"]).strip() if payload.get("token") else None,
            )
        except RequestBodyTooLarge as exc:
            response_json(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error_payload(str(exc), "invalid_request_error", "payload_too_large"))
            return
        except (ValueError, sqlite3.IntegrityError) as exc:
            response_json(self, HTTPStatus.BAD_REQUEST, error_payload(str(exc), "invalid_request_error", "bad_request"))
            return
        response_json(self, HTTPStatus.CREATED, {"user": user_to_public_dict(user), "token": token})

    def handle_reload_pool(self) -> None:
        if not self.authorized_admin():
            return
        try:
            env = dict(os.environ)
            env.update(read_dotenv_values())
            keys = split_csv_env(env.get("NVIDIA_API_KEYS", ""))
            if not keys:
                raise ConfigError("NVIDIA_API_KEYS is required.")
            accounts = load_account_pool(env)
        except ConfigError as exc:
            response_json(self, HTTPStatus.BAD_REQUEST, error_payload(str(exc), "invalid_request_error", "bad_request"))
            return
        added = self.proxy.pool.add_keys(keys)
        self.config.api_keys = self.proxy.pool.keys
        self.config.account_credentials = accounts
        response_json(
            self,
            HTTPStatus.OK,
            {
                "added_key_count": added,
                "key_count": self.proxy.pool.size,
                "account_count": len(accounts),
                "enabled_account_count": sum(1 for account in accounts if account.enabled),
            },
        )

    def handle_update_user(self, path: str) -> None:
        if not self.authorized_admin():
            return
        try:
            user_id = int(path.rsplit("/", 1)[-1])
            payload = read_json_body(self, self.config.max_request_body_bytes)
            user = self.store.update_user(user_id, payload)
        except RequestBodyTooLarge as exc:
            response_json(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error_payload(str(exc), "invalid_request_error", "payload_too_large"))
            return
        except ValueError as exc:
            response_json(self, HTTPStatus.BAD_REQUEST, error_payload(str(exc), "invalid_request_error", "bad_request"))
            return
        if user is None:
            response_json(self, HTTPStatus.NOT_FOUND, error_payload("User not found.", "invalid_request_error", "not_found"))
            return
        response_json(self, HTTPStatus.OK, {"user": user_to_public_dict(user)})

    def handle_delete_user(self, path: str) -> None:
        if not self.authorized_admin():
            return
        try:
            user_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            response_json(self, HTTPStatus.BAD_REQUEST, error_payload("Invalid user id.", "invalid_request_error", "bad_request"))
            return
        self.store.delete_user(user_id)
        response_json(self, HTTPStatus.OK, {"deleted": True})

    def authorized_admin(self) -> bool:
        actual = admin_token_from_request(self)
        if not hmac.compare_digest(actual, self.config.admin_token):
            response_json(self, HTTPStatus.UNAUTHORIZED, error_payload("Admin unauthorized.", "authentication_error", "unauthorized"))
            return False
        return True

    def authorized_user(self) -> Optional[ApiUser]:
        token = extract_bearer_token(self.headers)
        if not token:
            response_json(self, HTTPStatus.UNAUTHORIZED, error_payload("Bearer token is required.", "authentication_error", "unauthorized"))
            return None
        user = self.store.get_user_by_token(token)
        if user is None:
            response_json(self, HTTPStatus.UNAUTHORIZED, error_payload("Invalid user token.", "authentication_error", "unauthorized"))
            return None
        return user

    def admin_summary(self) -> Dict[str, Any]:
        return {
            "summary": self.store.summary(),
            "users": self.store.list_users(),
            "pool": self.proxy.pool.snapshot(),
            "accounts": account_pool_snapshot(self.config.account_credentials),
            "models": MODEL_LIST,
            "upstream_url": self.config.upstream_url,
        }

    def redirect(self, target: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", target)
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.config.access_log_health:
            parsed = urlparse(getattr(self, "path", ""))
            if parsed.path.rstrip("/") == "/health":
                return
        logging.info("%s - %s", self.address_string(), fmt % args)


def render_dashboard(store: UsageStore, pool: ApiKeyPool, config: AppConfig) -> str:
    summary = store.summary()
    users = store.list_users()
    pool_rows = pool.snapshot()
    account_rows = account_pool_snapshot(config.account_credentials)

    user_rows = "\n".join(
        f"""
        <tr>
          <td>{user['id']}</td>
          <td>{html.escape(user['name'])}</td>
          <td>{'启用' if user['enabled'] else '停用'}</td>
          <td>{format_number(user['used_tokens'])}</td>
          <td>{format_quota(user['quota_tokens'])}</td>
          <td>{format_quota(user['remaining_tokens'])}</td>
          <td>{format_number(user['request_count'])}</td>
          <td>{user['avg_latency_ms']} ms</td>
        </tr>
        """
        for user in users
    )
    model_rows = "\n".join(
        f"""
        <tr>
          <td>{html.escape(str(row['model']))}</td>
          <td>{format_number(row['request_count'])}</td>
          <td>{format_number(row['total_tokens'])}</td>
          <td>{round(float(row['avg_latency_ms'] or 0), 1)} ms</td>
        </tr>
        """
        for row in summary["models"]
    )
    pool_table_rows = "\n".join(
        f"""
        <tr>
          <td>{row['id']}</td>
          <td>{format_number(row['success_count'])}</td>
          <td>{format_number(row['fail_count'])}</td>
          <td>{row['in_flight']} / {row['max_in_flight']}</td>
          <td>{row['cooldown_seconds_remaining']} s</td>
          <td>{html.escape(row['last_error'])}</td>
        </tr>
        """
        for row in pool_rows
    )
    account_table_rows = "\n".join(
        f"""
        <tr>
          <td>{row['id']}</td>
          <td>{html.escape(str(row['email']))}</td>
          <td>{'enabled' if row['enabled'] else 'disabled'}</td>
          <td>{html.escape(str(row['note']))}</td>
        </tr>
        """
        for row in account_rows
    )
    recent_rows = "\n".join(
        f"""
        <tr>
          <td>{html.escape(str(row['created_at']))}</td>
          <td>{html.escape(str(row['user_name'] or 'deleted'))}</td>
          <td>{html.escape(str(row['model']))}</td>
          <td>{row['status_code']}</td>
          <td>{format_number(row['total_tokens'])}</td>
          <td>{row['latency_ms']} ms</td>
          <td>{html.escape(str(row['upstream_key_id']))}</td>
        </tr>
        """
        for row in summary["recent_requests"]
    )

    success_rate = 100.0
    if summary["request_count"]:
        success_rate = summary["success_count"] * 100 / summary["request_count"]

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>sub2api NVIDIA Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --surface: #ffffff;
      --line: #d8dee8;
      --text: #17202a;
      --muted: #627084;
      --accent: #1f7a5c;
      --accent-2: #2656a3;
      --warn: #a36014;
      --danger: #b3261e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, "Microsoft YaHei", sans-serif;
      line-height: 1.45;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--surface);
      padding: 18px 28px;
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
    }}
    h1 {{ margin: 0; font-size: 22px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 17px; letter-spacing: 0; }}
    main {{ width: min(1420px, calc(100% - 32px)); margin: 20px auto 40px; }}
    .subtle {{ color: var(--muted); font-size: 13px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .metric, section {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .metric {{ padding: 16px; min-height: 96px; }}
    .metric strong {{ display: block; font-size: 26px; margin-top: 8px; }}
    section {{ padding: 16px; margin-top: 16px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; background: #fbfcfd; }}
    form {{
      display: grid;
      grid-template-columns: 1.2fr 1fr 1.6fr auto;
      gap: 10px;
      align-items: end;
      margin-top: 10px;
    }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 12px; }}
    input {{
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      font-size: 14px;
      color: var(--text);
      background: #fff;
    }}
    button {{
      height: 36px;
      border: 0;
      border-radius: 6px;
      padding: 0 14px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }}
    code {{
      background: #eef2f7;
      border-radius: 4px;
      padding: 2px 5px;
    }}
    .ok {{ color: var(--accent); }}
    .warn {{ color: var(--warn); }}
    .danger {{ color: var(--danger); }}
    #created-token {{
      margin-top: 10px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
      word-break: break-all;
      display: none;
    }}
    @media (max-width: 900px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      .grid {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      form {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>sub2api NVIDIA Dashboard</h1>
      <div class="subtle">OpenAI-compatible endpoint: <code>http://{html.escape(config.bind_host)}:{config.port}/v1</code></div>
    </div>
    <div class="subtle">Upstream: {html.escape(config.upstream_url)} · Keys: {pool.size} · Accounts: {len(account_rows)}</div>
  </header>
  <main>
    <div class="grid">
      <div class="metric"><span class="subtle">总请求</span><strong>{format_number(summary['request_count'])}</strong></div>
      <div class="metric"><span class="subtle">总 Token</span><strong>{format_number(summary['total_tokens'])}</strong></div>
      <div class="metric"><span class="subtle">平均响应</span><strong>{summary['avg_latency_ms']} ms</strong></div>
      <div class="metric"><span class="subtle">成功率</span><strong>{round(success_rate, 1)}%</strong></div>
      <div class="metric"><span class="subtle">总余额</span><strong>{format_number(summary['balance_tokens'])}</strong></div>
      <div class="metric"><span class="subtle">活跃用户</span><strong>{format_number(summary['active_users'])}</strong></div>
      <div class="metric"><span class="subtle">Prompt Token</span><strong>{format_number(summary['prompt_tokens'])}</strong></div>
      <div class="metric"><span class="subtle">Completion Token</span><strong>{format_number(summary['completion_tokens'])}</strong></div>
      <div class="metric"><span class="subtle">失败请求</span><strong>{format_number(summary['error_count'])}</strong></div>
      <div class="metric"><span class="subtle">NVIDIA Accounts</span><strong>{format_number(len(account_rows))}</strong></div>
    </div>

    <section>
      <h2>创建用户 Token</h2>
      <div class="subtle">quota 填 -1 表示不限额；新 token 只显示一次。</div>
      <form id="create-user-form">
        <label>用户名称<input name="name" required placeholder="alice"></label>
        <label>Token 配额<input name="quota_tokens" required type="number" value="1000000"></label>
        <label>备注<input name="note" placeholder="team / usage note"></label>
        <button type="submit">创建</button>
      </form>
      <div id="created-token"></div>
    </section>

    <section>
      <h2>用户与余额</h2>
      <table>
        <thead><tr><th>ID</th><th>名称</th><th>状态</th><th>已用 Token</th><th>配额</th><th>余额</th><th>请求数</th><th>平均响应</th></tr></thead>
        <tbody>{user_rows or '<tr><td colspan="8">暂无用户</td></tr>'}</tbody>
      </table>
    </section>

    <section>
      <h2>模型用量</h2>
      <table>
        <thead><tr><th>模型</th><th>请求数</th><th>Token</th><th>平均响应</th></tr></thead>
        <tbody>{model_rows or '<tr><td colspan="4">暂无请求</td></tr>'}</tbody>
      </table>
    </section>

    <section>
      <h2>上游 Key 池</h2>
      <table>
        <thead><tr><th>Key</th><th>成功</th><th>失败</th><th>In Flight</th><th>冷却</th><th>最近错误</th></tr></thead>
        <tbody>{pool_table_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>NVIDIA Account Pool</h2>
      <table>
        <thead><tr><th>ID</th><th>Email</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>{account_table_rows or '<tr><td colspan="4">No accounts configured</td></tr>'}</tbody>
      </table>
    </section>

    <section>
      <h2>最近请求</h2>
      <table>
        <thead><tr><th>时间</th><th>用户</th><th>模型</th><th>状态</th><th>Token</th><th>响应</th><th>上游 Key</th></tr></thead>
        <tbody>{recent_rows or '<tr><td colspan="7">暂无请求</td></tr>'}</tbody>
      </table>
    </section>
  </main>
  <script>
    const token = new URLSearchParams(location.search).get('token') || localStorage.getItem('adminToken') || '';
    if (token) localStorage.setItem('adminToken', token);
    document.getElementById('create-user-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.target).entries());
      data.quota_tokens = Number(data.quota_tokens);
      const res = await fetch('/api/admin/users', {{
        method: 'POST',
        headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }},
        body: JSON.stringify(data)
      }});
      const payload = await res.json();
      const box = document.getElementById('created-token');
      box.style.display = 'block';
      if (!res.ok) {{
        box.textContent = payload.error ? payload.error.message : '创建失败';
        return;
      }}
      box.textContent = '';
      const strong = document.createElement('strong');
      strong.textContent = '新用户 Token:';
      const code = document.createElement('code');
      code.textContent = payload.token || '';
      box.append(strong, ' ', code);
    }});
  </script>
</body>
</html>"""


def format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def format_quota(value: int) -> str:
    if int(value) < 0:
        return "不限"
    return format_number(value)


def make_handler(proxy: NvidiaProxy, store: UsageStore, config: AppConfig) -> type:
    class Handler(ProxyHandler):
        pass

    Handler.proxy = proxy
    Handler.store = store
    Handler.config = config
    return Handler


def bootstrap_default_user(store: UsageStore, token: str, quota_tokens: int) -> None:
    if store.has_users():
        return
    try:
        user, _ = store.create_user("default", quota_tokens=quota_tokens, note="Bootstrap default client token", token=token)
        logging.info("Created default client user id=%s from DEFAULT_CLIENT_TOKEN.", user.id)
    except sqlite3.IntegrityError:
        logging.info("Default client token already exists.")


def serve_forever(config: AppConfig) -> None:
    store = UsageStore(config.database_path)
    default_client_token = os.environ.get("DEFAULT_CLIENT_TOKEN", "").strip() or os.environ.get("SUB2API_ACCESS_TOKEN", "").strip()
    if default_client_token:
        quota_tokens = int(os.environ.get("DEFAULT_CLIENT_QUOTA_TOKENS", "-1"))
        bootstrap_default_user(store, default_client_token, quota_tokens)
    proxy = NvidiaProxy(config, store)
    handler_class = make_handler(proxy, store, config)
    server = ThreadingHTTPServer((config.bind_host, config.port), handler_class)
    logging.info("Serving on http://%s:%s", config.bind_host, config.port)
    logging.info("Dashboard: http://%s:%s/dashboard", config.bind_host, config.port)
    logging.info("Loaded %s NVIDIA API keys.", proxy.pool.size)
    logging.info("Supported models: %s", ", ".join(model["id"] for model in MODEL_LIST))
    server.serve_forever()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAI-compatible NVIDIA NIM sub2api gateway.")
    parser.add_argument("--check-config", action="store_true", help="Load configuration and exit.")
    parser.add_argument("--create-user", metavar="NAME", help="Create a client API token and exit.")
    parser.add_argument("--quota", type=int, default=-1, help="Token quota for --create-user. -1 means unlimited.")
    parser.add_argument("--note", default="", help="User note for --create-user.")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_arg_parser().parse_args()
    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    if args.check_config:
        print(
            json.dumps(
                {
                    "bind_host": config.bind_host,
                    "port": config.port,
                    "database_path": config.database_path,
                    "upstream_url": config.upstream_url,
                    "key_count": len(config.api_keys),
                    "account_count": len(config.account_credentials),
                    "enabled_account_count": sum(1 for account in config.account_credentials if account.enabled),
                    "key_max_in_flight": config.key_max_in_flight,
                    "key_queue_wait_seconds": config.key_queue_wait_seconds,
                    "max_request_body_bytes": config.max_request_body_bytes,
                    "access_log_health": config.access_log_health,
                    "models": [model["id"] for model in MODEL_LIST],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.create_user:
        store = UsageStore(config.database_path)
        user, token = store.create_user(args.create_user, args.quota, args.note)
        print(json.dumps({"user": user_to_public_dict(user), "token": token}, ensure_ascii=False, indent=2))
        return

    serve_forever(config)


if __name__ == "__main__":
    main()
