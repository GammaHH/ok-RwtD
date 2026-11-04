"""Discord delivery only. No OK-Script imports, capture loops or game inputs.

Direct webhook credentials come exclusively from RWTD_DISCORD_WEBHOOK_URL.
An explicit task callback can instead submit through configured OK notifications.
Notification history
and the cooldown ledger stay in the allowed docs/rwtd/notifications/ tree.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from src.rwtd.notification_policy import exception_fingerprint, static_configuration_error

ENV_NAME = "RWTD_DISCORD_WEBHOOK_URL"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 8 * 1024 * 1024
_WEBHOOK = re.compile(r"/api(?:/v\d+)?/webhooks/\d{16,22}/[A-Za-z0-9_.-]{30,200}\Z")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def storage_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_relative_to(root / "docs/rwtd/notifications") or path.resolve() != path:
        raise ValueError("Notification storage escapes allowed directory or uses a link")
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("Notification storage uses a link")
    return path


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_png(data: bytes) -> tuple[int, int]:
    """Check signature, chunk boundaries, checksums and required image chunks."""
    if not data.startswith(PNG_SIGNATURE) or len(data) > MAX_PNG_BYTES:
        raise ValueError("Invalid or oversized PNG")
    cursor, dimensions, has_pixels = 8, None, False
    while cursor + 12 <= len(data):
        length = struct.unpack(">I", data[cursor:cursor + 4])[0]
        end = cursor + 12 + length
        if end > len(data):
            raise ValueError("Truncated PNG")
        kind = data[cursor + 4:cursor + 8]
        chunk = data[cursor + 8:cursor + 8 + length]
        crc = struct.unpack(">I", data[end - 4:end])[0]
        if zlib.crc32(kind + chunk) & 0xffffffff != crc:
            raise ValueError("Invalid PNG checksum")
        if cursor == 8:
            if kind != b"IHDR" or length != 13:
                raise ValueError("Missing PNG header")
            dimensions = struct.unpack(">II", chunk[:8])
            if not all(0 < size <= 32768 for size in dimensions):
                raise ValueError("Invalid PNG dimensions")
        elif kind == b"IHDR":
            raise ValueError("Duplicate PNG header")
        if kind == b"IDAT" and length:
            has_pixels = True
        if kind == b"IEND":
            if length or end != len(data) or not dimensions or not has_pixels:
                raise ValueError("Invalid PNG end")
            return dimensions
        cursor = end
    raise ValueError("Incomplete PNG")


def _post(endpoint: str, payload: dict, png: bytes | None):
    import requests
    with requests.Session() as session:
        if png is None:
            return session.post(endpoint, json=payload, timeout=(5, 15), allow_redirects=False)
        return session.post(
            endpoint, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
            files={"files[0]": ("exception.png", png, "image/png")},
            timeout=(5, 15), allow_redirects=False,
        )


def _seconds(value, fallback=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else fallback
    except (TypeError, ValueError):
        return fallback


class DiscordNotifier:
    def __init__(self, project_root: Path, *, cooldown_seconds=120,
                 duplicate_seconds=900, transport=None, clock=None, native_sender=None):
        self.root = Path(project_root).resolve()
        self.cooldown = _seconds(cooldown_seconds, 120)
        self.duplicate_window = _seconds(duplicate_seconds, 900)
        self._transport = transport or _post
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._native_sender = native_sender

    @staticmethod
    def _endpoint():
        raw = os.environ.get(ENV_NAME, "").strip()
        if not raw:
            return None, "webhook_not_configured", ""
        try:
            url = urlsplit(raw)
            valid = (url.scheme == "https" and url.netloc == "discord.com"
                     and not url.query and not url.fragment and _WEBHOOK.fullmatch(url.path))
        except ValueError:
            valid = False
        if not valid:
            return None, "invalid_webhook_configuration", ""
        return raw + "?wait=true", None, url.path.rsplit("/", 1)[-1]

    def send(self, description: str, *, png_path: Path | None = None,
             dedupe_key: str | None = None, exception: dict | None = None) -> dict:
        """One bounded request; no retry. Returns only safe, serializable evidence.

        Supply a stable task/category/step key; avoid timestamps in dedupe_key.
        Reserve cooldown before sending, including uncertain network outcomes,
        to avoid resending a message that Discord might already have received.
        """
        endpoint, problem, token = self._endpoint()
        text = _URL_IN_TEXT.sub("[URL redacted]", str(description))
        if token:
            text = text.replace(token, "[credential redacted]")
        text = text[:1800].strip()
        persistent = exception is not None
        fingerprint = (exception_fingerprint(exception["category"], exception["error_identifier"],
                       exception["device_identifier"], exception["details"]) if persistent else
                       hashlib.sha256((dedupe_key or text).encode("utf-8")).hexdigest())
        record = {"timestamp_utc": utc_now(), "status": "pending", "description": text,
                  "fingerprint": fingerprint, "delivery_verified": False,
                  "attachment_requested": png_path is not None, "attempts": 0,
                  "persistent_exception": persistent}
        # Audit storage must be available before any network operation.
        try:
            history = storage_path(self.root, "docs/rwtd/notifications")
            history.mkdir(parents=True, exist_ok=True)
            audit = history / ("notification_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
                               + "_" + uuid.uuid4().hex[:8] + ".json")
            write_json(audit, record)
        except (OSError, ValueError):
            return dict(record, status="audit_storage_unavailable")

        def finish(status, **fields):
            record.update(status=status, **fields)
            try:
                write_json(audit, record)
                from src.rwtd.android_core.record_retention import enforce
                enforce(self.root,'notifications','docs/rwtd/notifications',protected={audit.name})
            except OSError:
                record["audit_update_failed"] = True
            return dict(record, audit_path=str(audit))

        if not text:
            return finish("empty_description")
        policy = exception or {"category": "", "error_identifier": "", "details": text}
        if static_configuration_error(policy["category"], policy["error_identifier"], policy["details"]):
            return finish("static_configuration_local_only")
        use_native = problem == 'webhook_not_configured' and callable(self._native_sender)
        if problem and not use_native:
            return finish(problem)
        png = None
        if png_path is not None:
            try:
                if Path(png_path).stat().st_size > MAX_PNG_BYTES:
                    return finish("attachment_too_large")
                png = Path(png_path).read_bytes()
                dimensions = validate_png(png)
                record.update(attachment_bytes=len(png), attachment_size=list(dimensions),
                              attachment_sha256=hashlib.sha256(png).hexdigest())
            except (OSError, ValueError):
                return finish("invalid_png_attachment")

        # Exclusive filesystem lock also coordinates separate manual test processes.
        # A crash leaves the lock in place: fail closed, never automatically delete it.
        with self._lock:
            try:
                state_dir = storage_path(self.root, "docs/rwtd/notifications/.state")
                lock_path = storage_path(self.root, "docs/rwtd/notifications/.state/rwtd_discord.lock")
                state_path = storage_path(self.root, "docs/rwtd/notifications/.state/rwtd_discord_state.json")
                state_dir.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return finish("notification_service_busy")
            except (OSError, ValueError):
                return finish("cooldown_storage_unavailable")
            try:
                os.close(fd)
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                    if (not isinstance(state, dict) or not isinstance(state.get("fingerprints", {}), dict)
                            or not isinstance(state.get("exception_fingerprints", {}), dict)):
                        return finish("invalid_cooldown_state")
                    now = self._clock()
                    stamps = {key: _seconds(value) for key, value in state.get("fingerprints", {}).items()
                              if now - _seconds(value) < self.duplicate_window}
                    from src.rwtd.android_core.record_retention import bounded_stamps
                    exceptions = bounded_stamps(self.root,{key:_seconds(value) for key,value in state.get('exception_fingerprints',{}).items()},now)
                    stamps = bounded_stamps(self.root, stamps, now)
                    if stamps != state.get('fingerprints', {}) or exceptions != state.get('exception_fingerprints', {}):
                        state.update(fingerprints=stamps, exception_fingerprints=exceptions)
                        write_json(state_path, state)
                    if fingerprint in stamps or (persistent and fingerprint in exceptions):
                        return finish("duplicate_suppressed")
                    next_allowed = max(_seconds(state.get("next_allowed")),
                                       _seconds(state.get("rate_limited_until")))
                    if now < next_allowed:
                        return finish("cooldown_suppressed", retry_after_seconds=round(next_allowed - now, 3))
                    stamps[fingerprint] = now
                    if persistent:
                        exceptions[fingerprint] = now
                    stamps=bounded_stamps(self.root,stamps,now)
                    exceptions=bounded_stamps(self.root,exceptions,now)
                    state = {"next_allowed": now + self.cooldown,
                             "rate_limited_until": 0, "fingerprints": stamps,
                             "exception_fingerprints": exceptions}
                    write_json(state_path, state)
                    record["attempts"] = 1
                    write_json(audit, record)
                except (OSError, ValueError, TypeError):
                    return finish("invalid_cooldown_state")
                payload = {"content": text, "allowed_mentions": {"parse": []}}
                if png is not None:
                    payload["attachments"] = [{"id": 0, "filename": "exception.png"}]
                try:
                    if use_native:
                        # Explicit images from the analyzed frame; never capture here.
                        # Submission is not a Discord delivery acknowledgement.
                        self._native_sender(text, png)
                        return finish('native_submitted', channel='ok_native',
                                      attachment_submitted=png is not None)
                    response = self._transport(endpoint, payload, png)
                    status = response.status_code
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
                    if status == 429:
                        delay = _seconds(body.get("retry_after"), 60)
                        state["rate_limited_until"] = self._clock() + max(delay, 1)
                        write_json(state_path, state)
                        return finish("discord_rate_limited", http_status=status, retry_after_seconds=delay)
                    if status != 200:
                        return finish("discord_http_error", http_status=status)
                    message_id = str(body.get("id", ""))
                    channel_id = str(body.get("channel_id", ""))
                    if not message_id.isdigit() or not channel_id.isdigit() or body.get("content") != text:
                        return finish("discord_response_unverified", http_status=status)
                    attachments = body.get("attachments", [])
                    attached = (isinstance(attachments, list) and any(
                        isinstance(item, dict) and item.get("filename") == "exception.png"
                        and item.get("size") == len(png) and str(item.get("id", "")).isdigit()
                        for item in attachments)) if png is not None else False
                    if png is not None and not attached:
                        return finish("discord_attachment_unverified", http_status=status,
                                      message_id=message_id, channel_id=channel_id)
                    return finish("delivered", http_status=status, message_id=message_id,
                                  channel_id=channel_id, delivery_verified=True, attachment_verified=attached)
                except Exception:
                    # Never serialize response bodies, request objects or exception
                    # strings: HTTP errors can include the credential-bearing URL.
                    return finish("transport_failure_delivery_unknown")
            finally:
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    # Leaving the lock makes the next call fail closed. Do not
                    # raise an exception into the caller after Discord accepted.
                    record["lock_cleanup_failed"] = True


_services = {}
_services_lock = threading.Lock()


def get_notifier(project_root: Path) -> DiscordNotifier:
    """Reuse this object across future RwtD tasks; it never captures a frame."""
    root = Path(project_root).resolve()
    with _services_lock:
        if root not in _services:
            _services[root] = DiscordNotifier(root)
        return _services[root]
