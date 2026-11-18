#!/usr/bin/env python3
"""AML Add/Search adapter for MemOS Cloud (Textual track)."""

from __future__ import annotations

import hashlib
import hmac
from email.utils import parsedate_to_datetime
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
BASE_URL = os.environ.get("MEMOS_BASE_URL", "https://memos.memtensor.cn/api/openmem/v1").rstrip("/")
MEMOS_API_KEY = os.environ.get("MEMOS_API_KEY", "")
AML_API_KEY = os.environ.get("AML_API_KEY", "")
DB_PATH = Path(os.environ.get("DB_PATH", "./data/idempotency.sqlite3"))
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "1700"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(10 * 1024 * 1024)))
MEMOS_SEARCH_LIMIT = max(1, min(25, int(os.environ.get("MEMOS_SEARCH_LIMIT", "25"))))
MAX_ADD_ESTIMATED_TOKENS = max(256, int(os.environ.get("MAX_ADD_ESTIMATED_TOKENS", "30000")))
LEGACY_ADD_ESTIMATED_TOKENS = max(256, int(os.environ.get("LEGACY_ADD_ESTIMATED_TOKENS", "6000")))
MAX_ADD_MESSAGES_PER_CALL = max(1, int(os.environ.get("MAX_ADD_MESSAGES_PER_CALL", "10")))
MAX_SEARCH_ESTIMATED_TOKENS = max(256, int(os.environ.get("MAX_SEARCH_ESTIMATED_TOKENS", "6000")))
MAX_ADD_CONCURRENCY = max(1, min(4, int(os.environ.get("MAX_ADD_CONCURRENCY", "2"))))
ADD_QUEUE_TIMEOUT = max(1, int(os.environ.get("ADD_QUEUE_TIMEOUT_SECONDS", "1700")))
ADD_MIN_INTERVAL = max(0.0, float(os.environ.get("ADD_MIN_INTERVAL_SECONDS", "1")))
ADD_RETRY_AFTER = max(1, min(60, int(os.environ.get("ADD_RETRY_AFTER_SECONDS", "60"))))
ADD_UPSTREAM_RETRIES = max(0, min(10, int(os.environ.get("ADD_UPSTREAM_RETRIES", "3"))))
MAX_SEARCH_CONCURRENCY = max(1, min(4, int(os.environ.get("MAX_SEARCH_CONCURRENCY", "1"))))
SEARCH_QUEUE_TIMEOUT = max(1, int(os.environ.get("SEARCH_QUEUE_TIMEOUT_SECONDS", "1700")))
SEARCH_MIN_INTERVAL = max(0.0, float(os.environ.get("SEARCH_MIN_INTERVAL_SECONDS", "1")))
SEARCH_RETRY_AFTER = max(1, min(60, int(os.environ.get("SEARCH_RETRY_AFTER_SECONDS", "60"))))
SEARCH_UPSTREAM_RETRIES = max(0, min(10, int(os.environ.get("SEARCH_UPSTREAM_RETRIES", "3"))))

# MemOS caps each memory view at 25 results. Querying disjoint view groups lets
# an AML top_k=100 request retrieve up to 100+ distinct candidates without
# repeating the same search four times and receiving duplicate top-25 rows.
SEARCH_VIEW_GROUPS = (
    ("detail_factual",),
    ("preference", "profile"),
    ("event",),
    ("tool_memory", "skill"),
)

_db_lock = threading.Lock()
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}
_add_slots = threading.BoundedSemaphore(MAX_ADD_CONCURRENCY)
_add_gate = threading.Condition()
_add_next_start = 0.0
_add_cooldown_until = 0.0
_search_slots = threading.BoundedSemaphore(MAX_SEARCH_CONCURRENCY)
_search_gate = threading.Condition()
_search_next_start = 0.0
_search_cooldown_until = 0.0


class AdapterError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        retry_after: str | None = None,
        upstream_code: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.upstream_code = upstream_code


def valid_retry_after(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or "\r" in candidate or "\n" in candidate:
        return None
    if candidate.isascii() and candidate.isdecimal():
        return candidate
    try:
        parsedate_to_datetime(candidate)
        return candidate
    except (TypeError, ValueError, OverflowError):
        return None


def retry_after_seconds(value: str | None) -> int:
    if value is None:
        return SEARCH_RETRY_AFTER
    candidate = valid_retry_after(value)
    if candidate is None:
        return SEARCH_RETRY_AFTER
    if candidate.isascii() and candidate.isdecimal():
        return max(1, min(60, int(candidate)))
    try:
        target = parsedate_to_datetime(candidate).timestamp()
        return max(1, min(60, int(target - time.time() + 0.999)))
    except (TypeError, ValueError, OverflowError, OSError):
        return SEARCH_RETRY_AFTER


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS adds (request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, response TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS add_parts ("
        "request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, part_id TEXT NOT NULL, "
        "part_hash TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "PRIMARY KEY(request_id, part_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS add_plans ("
        "request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, "
        "max_estimated_tokens INTEGER NOT NULL, created_at INTEGER NOT NULL)"
    )
    return conn


def upstream_error(raw: bytes) -> tuple[str | None, str]:
    detail = raw[:2048].decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, detail[:500]
    if not isinstance(payload, dict):
        return None, detail[:500]
    code = payload.get("code")
    code_text = str(code) if isinstance(code, (str, int)) else None
    message = payload.get("message", payload.get("msg", payload.get("detail", detail)))
    if not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=False)
    return code_text, message[:500]


def mapped_upstream_status(code: str | None, http_status: int | None = None) -> int:
    if code == "40305":
        return 413
    if code in ("40300", "40304", "40309"):
        return 429
    if http_status == 429:
        return 429
    if http_status is not None and http_status >= 500:
        return 502
    if code is not None and code.startswith("5"):
        return 502
    return 422


def upstream(path: str, body: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
    if not MEMOS_API_KEY:
        raise AdapterError(503, "MemOS API is not configured")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/{path.lstrip('/')}",
        data=payload,
        headers={
            "Authorization": f"Token {MEMOS_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or UPSTREAM_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read(2048)
        code, detail = upstream_error(raw)
        status = mapped_upstream_status(code, exc.code)
        retry_after = valid_retry_after(exc.headers.get("Retry-After")) if exc.code == 429 else None
        label = f"MemOS error {code}" if code else f"MemOS returned HTTP {exc.code}"
        raise AdapterError(status, f"{label}: {detail}", retry_after, code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(502, f"MemOS request failed: {exc}") from exc
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(502, "MemOS returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise AdapterError(502, "MemOS returned an unsuccessful response")
    if result.get("code") != 0:
        code, detail = upstream_error(raw)
        raise AdapterError(mapped_upstream_status(code), f"MemOS error {code or 'unknown'}: {detail}", upstream_code=code)
    return result


def chat_time(timestamp: Any) -> str | None:
    if timestamp is None:
        return None
    if not isinstance(timestamp, (int, float)):
        raise AdapterError(422, "messages[].timestamp must be Unix milliseconds")
    try:
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError) as exc:
        raise AdapterError(422, "messages[].timestamp is out of range") from exc


def estimated_token_units(text: str) -> int:
    """Conservative tokenizer-independent estimate: 3 units ~= 1 token."""
    return sum(1 if ord(character) < 128 else 3 for character in text)


def split_text(text: str, max_estimated_tokens: int) -> list[str]:
    max_units = max(1, max_estimated_tokens) * 3
    if estimated_token_units(text) <= max_units:
        return [text]
    output: list[str] = []
    start = 0
    while start < len(text):
        units = 0
        end = start
        while end < len(text):
            weight = 1 if ord(text[end]) < 128 else 3
            if end > start and units + weight > max_units:
                break
            units += weight
            end += 1
        if end < len(text):
            lower_bound = start + max(1, (end - start) * 3 // 4)
            whitespace = max(text.rfind(" ", lower_bound, end), text.rfind("\n", lower_bound, end))
            if whitespace >= lower_bound:
                end = whitespace + 1
        output.append(text[start:end])
        start = end
    return output


def split_add_messages(
    messages: list[dict[str, Any]], max_estimated_tokens: int | None = None
) -> list[list[dict[str, Any]]]:
    token_limit = max_estimated_tokens or MAX_ADD_ESTIMATED_TOKENS
    expanded: list[dict[str, Any]] = []
    for message in messages:
        pieces = split_text(message["content"], token_limit)
        for piece in pieces:
            expanded.append({**message, "content": piece})

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_units = 0
    max_units = token_limit * 3
    for message in expanded:
        message_units = estimated_token_units(message["content"]) + 48
        if current and (
            len(current) >= MAX_ADD_MESSAGES_PER_CALL or current_units + message_units > max_units
        ):
            chunks.append(current)
            current = []
            current_units = 0
        current.append(message)
        current_units += message_units
    if current:
        chunks.append(current)
    return chunks


def get_add_plan(request_id: str, payload_hash: str) -> int:
    """Freeze splitting per request so interrupted legacy Adds remain resumable."""
    with _db_lock:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT payload_hash, max_estimated_tokens FROM add_plans WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row[0] != payload_hash:
                    raise AdapterError(409, "Add plan does not match the retried payload")
                return int(row[1])
            has_legacy_parts = conn.execute(
                "SELECT 1 FROM add_parts WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone() is not None
            token_limit = LEGACY_ADD_ESTIMATED_TOKENS if has_legacy_parts else MAX_ADD_ESTIMATED_TOKENS
            conn.execute(
                "INSERT INTO add_plans(request_id, payload_hash, max_estimated_tokens, created_at) "
                "VALUES (?, ?, ?, ?)",
                (request_id, payload_hash, token_limit, int(time.time())),
            )
            return token_limit


def wait_add_start() -> None:
    global _add_next_start
    with _add_gate:
        while True:
            now = time.monotonic()
            ready_at = max(_add_next_start, _add_cooldown_until)
            if ready_at <= now:
                _add_next_start = now + ADD_MIN_INTERVAL
                return
            _add_gate.wait(timeout=ready_at - now)


def add_cooldown(delay: int) -> None:
    global _add_cooldown_until
    with _add_gate:
        _add_cooldown_until = max(_add_cooldown_until, time.monotonic() + delay)
        _add_gate.notify_all()


def add_part_completed(request_id: str, payload_hash: str, part_id: str, part_hash: str) -> bool:
    with _db_lock:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT payload_hash, part_hash FROM add_parts WHERE request_id = ? AND part_id = ?",
                (request_id, part_id),
            ).fetchone()
    if row is None:
        return False
    if row != (payload_hash, part_hash):
        raise AdapterError(409, "completed Add part does not match the retried payload")
    return True


def record_add_part(request_id: str, payload_hash: str, part_id: str, part_hash: str) -> None:
    with _db_lock:
        with db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO add_parts(request_id, payload_hash, part_id, part_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (request_id, payload_hash, part_id, part_hash, int(time.time())),
            )


def split_failed_add_part(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body["messages"]
    if len(messages) > 1:
        middle = len(messages) // 2
        return [{**body, "messages": messages[:middle]}, {**body, "messages": messages[middle:]}]
    message = messages[0]
    estimated = max(1, (estimated_token_units(message["content"]) + 2) // 3)
    pieces = split_text(message["content"], max(1, estimated // 2))
    if len(pieces) < 2:
        return []
    return [{**body, "messages": [{**message, "content": piece}]} for piece in pieces]


def add_part(body: dict[str, Any], request_id: str, payload_hash: str, part_id: str) -> None:
    part_hash = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if add_part_completed(request_id, payload_hash, part_id, part_hash):
        return
    attempt = 0
    while True:
        wait_add_start()
        try:
            result = upstream("add/message", body)
        except AdapterError as exc:
            if exc.upstream_code == "40305":
                children = split_failed_add_part(body)
                if not children:
                    raise AdapterError(422, "MemOS rejected even the smallest Add fragment as too large") from exc
                for index, child in enumerate(children):
                    add_part(child, request_id, payload_hash, f"{part_id}.{index}")
                record_add_part(request_id, payload_hash, part_id, part_hash)
                return
            if exc.status not in (429, 502, 503, 504) or attempt >= ADD_UPSTREAM_RETRIES:
                raise
            if exc.upstream_code in ("40300", "40304"):
                raise
            delay = retry_after_seconds(exc.retry_after) if exc.status == 429 else min(30, 2 ** (attempt + 1))
            add_cooldown(delay)
            attempt += 1
            continue
        data = result.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise AdapterError(502, "MemOS did not confirm successful Add")
        if data.get("status") not in (None, "completed"):
            raise AdapterError(502, "MemOS Add did not complete synchronously")
        record_add_part(request_id, payload_hash, part_id, part_hash)
        return


def validate_add(payload: Any) -> tuple[dict[str, Any], str, str, str]:
    if not isinstance(payload, dict):
        raise AdapterError(422, "request body must be a JSON object")
    request_id = payload.get("request_id")
    user_id = payload.get("user_id")
    session_id = payload.get("session_id")
    messages = payload.get("messages")
    if not all(isinstance(value, str) and value for value in (request_id, user_id, session_id)):
        raise AdapterError(422, "request_id, user_id, and session_id must be non-empty strings")
    if not isinstance(messages, list) or not messages:
        raise AdapterError(422, "messages must be a non-empty array")
    converted = []
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            raise AdapterError(422, "each message must have role user or assistant")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AdapterError(422, "Textual track messages require non-empty string content")
        message = {"role": item["role"], "content": content}
        if item.get("timestamp") is not None:
            message["chat_time"] = chat_time(item["timestamp"])
        converted.append(message)
    return {"user_id": user_id, "conversation_id": session_id, "messages": converted, "async_mode": False}, request_id, user_id, session_id


def add(payload: Any) -> dict[str, Any]:
    body, request_id, user_id, session_id = validate_add(payload)
    payload_hash = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    while True:
        with _db_lock:
            with db_connect() as conn:
                row = conn.execute("SELECT payload_hash, response FROM adds WHERE request_id = ?", (request_id,)).fetchone()
        if row:
            if row[0] != payload_hash:
                raise AdapterError(409, "request_id was already used with a different payload")
            return json.loads(row[1])
        with _inflight_lock:
            event = _inflight.get(request_id)
            if event is None:
                event = threading.Event()
                _inflight[request_id] = event
                owner = True
            else:
                owner = False
        if owner:
            break
        event.wait(UPSTREAM_TIMEOUT + 30)

    try:
        token_limit = get_add_plan(request_id, payload_hash)
        if not _add_slots.acquire(timeout=ADD_QUEUE_TIMEOUT):
            raise AdapterError(429, "Add capacity is temporarily busy", str(ADD_RETRY_AFTER))
        try:
            chunks = split_add_messages(body["messages"], token_limit)
            for index, messages in enumerate(chunks):
                add_part({**body, "messages": messages}, request_id, payload_hash, str(index))
        finally:
            _add_slots.release()
        response = {"success": True, "request_id": request_id, "user_id": user_id, "session_id": session_id}
        with _db_lock:
            with db_connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO adds(request_id, payload_hash, response, created_at) VALUES (?, ?, ?, ?)",
                    (request_id, payload_hash, json.dumps(response), int(time.time())),
                )
        return response
    finally:
        with _inflight_lock:
            _inflight.pop(request_id, None)
            event.set()


def flatten_memories(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise AdapterError(502, "MemOS Search response has no data object")
    fields = (
        ("memory_detail_list", "memory_key", "memory_value"),
        ("preference_detail_list", "", "preference"),
        ("profile_detail_list", "", "memory"),
        ("event_detail_list", "event_key", "event_value"),
        ("tool_memory_detail_list", "", "tool_value"),
        ("skill_detail_list", "", "skill_value"),
    )
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for list_name, key_name, value_name in fields:
        records = data.get(list_name, []) or []
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            value = record.get(value_name)
            if isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if not isinstance(value, str) or not value.strip():
                continue
            key = record.get(key_name) if key_name else None
            content = f"{key}: {value}" if isinstance(key, str) and key.strip() else value
            identifier = record.get("id")
            identifier = identifier if isinstance(identifier, str) and identifier else "mem_" + hashlib.sha256(content.encode()).hexdigest()[:24]
            if identifier in seen:
                continue
            seen.add(identifier)
            item: dict[str, Any] = {"id": identifier, "content": content}
            score = record.get("relativity", record.get("confidence"))
            if isinstance(score, (int, float)):
                item["score"] = float(score)
            created_at = record.get("created_at", record.get("create_time"))
            if isinstance(created_at, str) and created_at:
                item["created_at"] = created_at
            output.append(item)
    output.sort(key=lambda item: item.get("score", 0), reverse=True)
    return output


def wait_search_start() -> None:
    global _search_next_start
    with _search_gate:
        while True:
            now = time.monotonic()
            ready_at = max(_search_next_start, _search_cooldown_until)
            if ready_at <= now:
                _search_next_start = now + SEARCH_MIN_INTERVAL
                return
            _search_gate.wait(timeout=ready_at - now)


def search_cooldown(delay: int) -> None:
    global _search_cooldown_until
    with _search_gate:
        _search_cooldown_until = max(_search_cooldown_until, time.monotonic() + delay)
        _search_gate.notify_all()


def merge_memories(groups: list[list[dict[str, Any]]], top_k: int) -> list[dict[str, Any]]:
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    order = 0
    for records in groups:
        for record in records:
            identifier = record["id"]
            previous = best.get(identifier)
            if previous is None or record.get("score", 0) > previous[1].get("score", 0):
                best[identifier] = (order if previous is None else previous[0], record)
            order += 1
    ranked = sorted(best.values(), key=lambda entry: (-entry[1].get("score", 0), entry[0]))
    return [record for _, record in ranked[:top_k]]


def search_group(query: str, user_id: str, views: tuple[str, ...], started: float) -> list[dict[str, Any]]:
    attempt = 0
    while True:
        wait_search_start()
        try:
            response = upstream(
                "search/memory",
                {
                    "user_id": user_id,
                    "query": query,
                    "memory_limit_number": MEMOS_SEARCH_LIMIT,
                    "relativity": 0,
                    "include_memory_view": list(views),
                },
                timeout=max(1, UPSTREAM_TIMEOUT - int(time.monotonic() - started)),
            )
        except AdapterError as exc:
            if exc.upstream_code == "40305":
                pieces = split_text(query, max(1, ((estimated_token_units(query) + 2) // 3) // 2))
                if len(pieces) < 2:
                    raise AdapterError(422, "MemOS rejected even the smallest Search fragment as too large") from exc
                return merge_memories(
                    [search_group(piece, user_id, views, started) for piece in pieces],
                    MEMOS_SEARCH_LIMIT,
                )
            if exc.status not in (429, 502, 503, 504) or attempt >= SEARCH_UPSTREAM_RETRIES:
                if exc.status == 429:
                    exc.retry_after = str(min(retry_after_seconds(exc.retry_after), 60))
                raise
            if exc.upstream_code in ("40300", "40304"):
                raise
            delay = retry_after_seconds(exc.retry_after) if exc.status == 429 else min(30, 2 ** (attempt + 1))
            search_cooldown(delay)
            attempt += 1
            continue
        return flatten_memories(response.get("data"))


def search(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AdapterError(422, "request body must be a JSON object")
    query = payload.get("query")
    user_id = payload.get("user_id")
    top_k = payload.get("top_k")
    if not isinstance(query, str) or not query.strip():
        raise AdapterError(422, "Textual track query must be a non-empty string")
    if not isinstance(user_id, str) or not user_id:
        raise AdapterError(422, "user_id must be a non-empty string")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0:
        raise AdapterError(422, "top_k must be a non-negative integer")
    if top_k == 0:
        return {"data": []}
    started = time.monotonic()
    if not _search_slots.acquire(timeout=SEARCH_QUEUE_TIMEOUT):
        raise AdapterError(429, "Search capacity is temporarily busy", str(SEARCH_RETRY_AFTER))
    try:
        query_pieces = split_text(query, MAX_SEARCH_ESTIMATED_TOKENS)
        groups = [
            search_group(query_piece, user_id, views, started)
            for query_piece in query_pieces
            for views in SEARCH_VIEW_GROUPS
        ]
    finally:
        _search_slots.release()
    return {"data": merge_memories(groups, top_k)}


class Handler(BaseHTTPRequestHandler):
    server_version = "MemOSAMLAdapter/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid logging request bodies, user IDs, credentials, or query text.
        print(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}", flush=True)

    def send_json(self, status: int, body: dict[str, Any], retry_after: str | None = None) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        self.end_headers()
        self.wfile.write(encoded)

    def authorized(self) -> bool:
        if not AML_API_KEY:
            return False
        supplied = self.headers.get("X-Api-Key", "")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") or auth.startswith("Token "):
            supplied = auth.split(" ", 1)[1]
        return hmac.compare_digest(supplied, AML_API_KEY)

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_json(404, {"detail": "not found"})
            return
        self.send_json(200, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path not in ("/add", "/search"):
            self.send_json(404, {"detail": "not found"})
            return
        if not self.authorized():
            self.send_json(401, {"detail": "invalid or unconfigured API key"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise AdapterError(413, "request body is empty or too large")
            payload = json.loads(self.rfile.read(length))
            result = add(payload) if self.path == "/add" else search(payload)
            self.send_json(200, result)
        except AdapterError as exc:
            self.send_json(exc.status, {"detail": str(exc)}, exc.retry_after)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"detail": "invalid JSON"})
        except (BrokenPipeError, ConnectionResetError):
            # The evaluator may abandon a request at its deadline. Do not emit
            # a misleading second 500 after the response socket is already gone.
            return
        except Exception:
            self.send_json(500, {"detail": "internal adapter error"})


if __name__ == "__main__":
    if not AML_API_KEY:
        print("AML_API_KEY is unset; Add/Search will reject requests until configured.", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
