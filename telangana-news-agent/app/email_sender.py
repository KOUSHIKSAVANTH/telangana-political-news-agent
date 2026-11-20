"""Email rendering and delivery via Gmail API (OAuth). Dry-run writes previews to disk."""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.settings import Settings

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
TEMPLATES = Path(__file__).parent / "templates"


def fmt_ist(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def article_link(row: dict[str, Any]) -> str:
    """The publisher's original link as received (never constructed)."""
    meta = row.get("raw_metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta.get("link") or row["canonical_url"]


_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]),
                   trim_blocks=True, lstrip_blocks=True)
_env.filters["ist"] = fmt_ist
_env.filters["link"] = article_link


def render(template: str, **ctx: Any) -> str:
    return _env.get_template(template).render(**ctx)


@dataclass
class SendResult:
    status: str  # sent | dry_run
    provider_message_id: str | None
    preview_path: str | None = None


class RecipientNotAllowed(RuntimeError):
    pass


class EmailSender:
    def __init__(self, settings: Settings):
        self.s = settings
        self._service = None

    def _gmail(self):
        if self._service is not None:
            return self._service
        from googleapiclient.discovery import build

        s = self.s
        if s.gmail_service_account_json and s.gmail_delegated_user:
            from google.oauth2 import service_account

            info = json.loads(s.gmail_service_account_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[GMAIL_SCOPE]).with_subject(s.gmail_delegated_user)
        elif s.gmail_client_id and s.gmail_client_secret and s.gmail_refresh_token:
            from google.oauth2.credentials import Credentials

            creds = Credentials(
                token=None, refresh_token=s.gmail_refresh_token, client_id=s.gmail_client_id,
                client_secret=s.gmail_client_secret, token_uri="https://oauth2.googleapis.com/token",
                scopes=[GMAIL_SCOPE],
            )
        else:
            raise RuntimeError("Gmail credentials missing: set GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN "
                               "or GMAIL_SERVICE_ACCOUNT_JSON + GMAIL_DELEGATED_USER")
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _check_recipients(self, recipients: list[str]) -> None:
        if not recipients:
            raise RecipientNotAllowed("no recipients configured")
        if not self.s.recipient_allowlist:
            raise RecipientNotAllowed("EMAIL_RECIPIENT_ALLOWLIST must be set before any live send")
        blocked = [r for r in recipients if r.lower() not in self.s.recipient_allowlist]
        if blocked:
            raise RecipientNotAllowed(f"recipients not on allowlist: {', '.join(blocked)}")

    def send(self, *, recipients: list[str], subject: str, html: str, text: str,
             idempotency_key: str) -> SendResult:
        if self.s.dry_run:
            self.s.output_dir.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", idempotency_key)
            html_path = self.s.output_dir / f"{stem}.html"
            html_path.write_text(html, encoding="utf-8")
            (self.s.output_dir / f"{stem}.txt").write_text(
                f"To: {', '.join(recipients) or '(none configured)'}\nSubject: {subject}\n\n{text}", encoding="utf-8")
            log.info("DRY RUN: wrote %s (subject: %s)", html_path, subject)
            return SendResult("dry_run", None, str(html_path))

        self._check_recipients(recipients)
        if not self.s.email_sender:
            raise RuntimeError("EMAIL_SENDER is not set")
        msg = EmailMessage()
        msg["From"] = self.s.email_sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg["X-Telangana-Agent-Key"] = idempotency_key
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        resp = self._gmail().users().messages().send(userId="me", body={"raw": raw}).execute()
        return SendResult("sent", resp.get("id"))
