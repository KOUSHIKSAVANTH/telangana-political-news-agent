"""Telegram delivery via the Bot API (free). Send-only: the bot never reads or answers messages.

Messages use Telegram's HTML subset (<b>, <i>, <a href>, <blockquote>). Long messages are split
at blank lines so no HTML tag is cut, staying under Telegram's 4096-character limit.
Dry-run writes the message parts to out/ instead of sending.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import time

import httpx

from app.email_sender import SendResult
from app.settings import Settings

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
LIMIT = 4000  # Telegram's hard limit is 4096; keep a margin


class ChatNotAllowed(RuntimeError):
    pass


def split_message(text: str, limit: int = LIMIT) -> list[tuple[str, str | None]]:
    """Split into (chunk, parse_mode) parts at blank lines. A single block that is still too
    long is sent as plain text (tags removed) and truncated, so it can never break HTML parsing."""
    parts: list[tuple[str, str | None]] = []
    current = ""
    blocks: list[str] = []
    for b in [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]:
        # A section heading travels with the first story under it, never alone at a message end.
        if blocks and "━━" in blocks[-1] and "\n" not in blocks[-1]:
            blocks[-1] = f"{blocks[-1]}\n\n{b}"
        else:
            blocks.append(b)
    for block in blocks:
        if len(block) > limit:
            if current:
                parts.append((current, "HTML"))
                current = ""
            plain = html_lib.unescape(re.sub(r"<[^>]+>", "", block))
            parts.append((plain[: limit - 1] + "…" if len(plain) > limit else plain, None))
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            parts.append((current, "HTML"))
            current = block
        else:
            current = candidate
    if current:
        parts.append((current, "HTML"))
    return parts


class TelegramSender:
    def __init__(self, settings: Settings, *, http: httpx.Client | None = None, max_wait_seconds: int = 30):
        self.s = settings
        self.http = http or httpx.Client(timeout=20)
        self.max_wait = max_wait_seconds

    @property
    def allowed_chats(self) -> set[str]:
        return {str(c) for c in (*self.s.telegram_team_chat_ids, *self.s.telegram_reviewer_chat_ids)}

    def _redact(self, msg: str) -> str:
        token = self.s.telegram_bot_token or ""
        return msg.replace(token, "<token>") if token else msg

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{API}/bot{self.s.telegram_bot_token}/{method}"
        for _ in range(3):
            try:
                resp = self.http.post(url, json=payload)
            except httpx.HTTPError as exc:  # the URL contains the token: never let it reach logs
                raise RuntimeError(self._redact(f"Telegram request failed: {type(exc).__name__}: {exc}")) from None
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 429:
                wait = int((data.get("parameters") or {}).get("retry_after", 5))
                if wait > self.max_wait:
                    raise RuntimeError(f"Telegram rate limit: retry after {wait}s")
                time.sleep(wait)
                continue
            if not data.get("ok"):
                raise RuntimeError(self._redact(
                    f"Telegram {method} failed ({resp.status_code}): {data.get('description') or resp.text[:200]}"))
            return data["result"]
        raise RuntimeError("Telegram rate limit persisted after retries")

    def send(self, *, recipients: list[str], subject: str, html: str, text: str,
             idempotency_key: str) -> SendResult:
        """Same interface as EmailSender.send. `recipients` are chat IDs; `html` is Telegram HTML."""
        parts = split_message(html)
        if self.s.dry_run:
            self.s.output_dir.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", idempotency_key)
            path = self.s.output_dir / f"{stem}.telegram.txt"
            body = "\n\n----- next message -----\n\n".join(p for p, _ in parts)
            path.write_text(f"To chats: {', '.join(recipients) or '(none configured)'}\n"
                            f"Parts: {len(parts)}\n\n{body}", encoding="utf-8")
            log.info("DRY RUN: wrote %s (%s)", path, subject)
            return SendResult("dry_run", None, str(path))

        if not self.s.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        if not recipients:
            raise ChatNotAllowed("no Telegram chat configured for this recipient group")
        blocked = [r for r in recipients if str(r) not in self.allowed_chats]
        if blocked:
            raise ChatNotAllowed(f"chat ids not configured: {', '.join(map(str, blocked))}")
        ids = []
        for chat in recipients:
            for chunk, mode in parts:
                payload = {"chat_id": chat, "text": chunk, "link_preview_options": {"is_disabled": True}}
                if mode:
                    payload["parse_mode"] = mode
                ids.append(str(self._call("sendMessage", payload).get("message_id")))
        return SendResult("sent", ",".join(ids))
