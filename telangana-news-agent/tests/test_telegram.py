"""Telegram channel: splitting, dry-run, live send (mocked), safety, and multi-channel delivery."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import telegram_sender
from app.database import Database
from app.email_sender import SendResult, render
from app.pipeline import deliver_all
from app.settings import _channels, get_settings
from app.telegram_sender import ChatNotAllowed, TelegramSender, split_message

TOKEN = "123456:SECRET-TOKEN-VALUE"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(telegram_sender.time, "sleep", lambda s: None)


@pytest.fixture
def settings(tmp_path):
    return replace(get_settings(), dry_run=False, output_dir=tmp_path / "out", telegram_bot_token=TOKEN,
                   telegram_team_chat_ids=["-1001"], telegram_reviewer_chat_ids=["-1002"],
                   team_recipients=["team@example.com"], reviewer_recipients=["rev@example.com"],
                   recipient_allowlist=["team@example.com", "rev@example.com"], email_sender="bot@example.com",
                   delivery_channels=["email", "telegram"])


def ok(message_id=1):
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


def sender(settings, handler):
    return TelegramSender(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_split_keeps_blocks_whole_and_under_limit():
    blocks = [f"<b>Item {i}</b>\n" + "x" * 300 for i in range(40)]
    parts = split_message("\n\n".join(blocks), limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 and mode == "HTML" for p, mode in parts)
    assert all(p.count("<b>") == p.count("</b>") for p, _ in parts)


def test_oversized_block_becomes_plain_text():
    (part, mode), = split_message("<b>" + "y" * 50 + "</b>", limit=20)
    assert mode is None and "<b>" not in part and len(part) <= 20


def test_dry_run_writes_file_and_sends_nothing(settings):
    s = replace(settings, dry_run=True)
    res = sender(s, lambda r: pytest.fail("must not call Telegram")).send(
        recipients=["-1001"], subject="s", html="<b>hi</b>", text="hi", idempotency_key="dryrun:tg:digest:x")
    assert res.status == "dry_run" and "<b>hi</b>" in open(res.preview_path, encoding="utf-8").read()


def test_live_send_uses_html_and_disables_previews(settings):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return ok(7)

    res = sender(settings, handler).send(recipients=["-1001"], subject="s", html="<b>hi</b>", text="hi",
                                         idempotency_key="k")
    assert res.status == "sent" and res.provider_message_id == "7"
    assert seen[0]["parse_mode"] == "HTML" and seen[0]["link_preview_options"]["is_disabled"] is True


def test_unconfigured_chat_is_refused(settings):
    with pytest.raises(ChatNotAllowed):
        sender(settings, lambda r: ok()).send(recipients=["-999"], subject="s", html="x", text="x",
                                              idempotency_key="k")


def test_rate_limit_waits_then_succeeds(settings):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}})
        return ok()

    sender(settings, handler).send(recipients=["-1001"], subject="s", html="x", text="x", idempotency_key="k")
    assert len(calls) == 2


def test_token_never_appears_in_errors(settings):
    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    with pytest.raises(RuntimeError) as exc:
        sender(settings, boom).send(recipients=["-1001"], subject="s", html="x", text="x", idempotency_key="k")
    assert TOKEN not in str(exc.value) and "<token>" in str(exc.value)


def test_api_error_redacted(settings):
    resp = httpx.Response(400, json={"ok": False, "description": f"bad request for {TOKEN}"})
    with pytest.raises(RuntimeError) as exc:
        sender(settings, lambda r: resp).send(recipients=["-1001"], subject="s", html="x", text="x",
                                              idempotency_key="k")
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("raw,expected", [("", ["email"]), ("telegram", ["telegram"]),
                                          ("Email, telegram", ["email", "telegram"])])
def test_channel_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("DELIVERY_CHANNELS", raw)
    assert _channels() == expected


def test_unknown_channel_rejected(monkeypatch):
    monkeypatch.setenv("DELIVERY_CHANNELS", "email,whatsapp")
    with pytest.raises(ValueError):
        _channels()


# ---------------------------------------------------------------- multi-channel delivery
class Recorder:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def send(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("down")
        return SendResult("sent", "id")


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 't.db'}")
    d.create_all()
    return d


def _send(settings, db, senders, key="alert:1:v0:high", group="team"):
    return deliver_all(settings, db, senders, key=key, type_="breaking", recipient_group=group, subject="s",
                       html="<p>e</p>", text="t", telegram_html="<b>t</b>", story_id=None)


def test_each_channel_gets_its_own_recipients_and_key(settings, db):
    email, tg = Recorder(), Recorder()
    _send(settings, db, {"email": email, "telegram": tg}, group="reviewer")
    assert email.calls[0]["recipients"] == ["rev@example.com"] and email.calls[0]["html"] == "<p>e</p>"
    assert tg.calls[0]["recipients"] == ["-1002"] and tg.calls[0]["html"] == "<b>t</b>"
    assert db.get_notification("alert:1:v0:high") and db.get_notification("tg:alert:1:v0:high")


def test_one_channel_failing_does_not_block_or_resend_the_other(settings, db):
    email, tg = Recorder(), Recorder(fail=True)
    with pytest.raises(RuntimeError, match="telegram"):
        _send(settings, db, {"email": email, "telegram": tg})
    tg.fail = False
    _send(settings, db, {"email": email, "telegram": tg})  # retry
    assert len(email.calls) == 1 and len(tg.calls) == 2


def test_alert_on_two_channels_counts_once(settings, db):
    _send(settings, db, {"email": Recorder(), "telegram": Recorder()})
    assert db.count_alerts_since(datetime.now(UTC) - timedelta(hours=1)) == 1


def test_telegram_templates_render_and_escape():
    story = {"id": 1, "title": "Rain & floods <Hyderabad>", "summary": "Heavy rain.", "why_it_matters": None,
             "priority": "high", "verification_status": "confirmed", "districts": ["Hyderabad"],
             "analysis_source": "groq"}
    art = {"title": "Rain", "publisher": "Pub", "language": "en", "title_en": None,
           "published_at": datetime(2026, 11, 20, tzinfo=UTC), "raw_metadata": {"link": "https://ex.com/a?b=1&c=2"},
           "canonical_url": "https://ex.com/a"}
    evidence = type("E", (), {"independent_sources": 2, "has_official": False})()
    alert = render("telegram_breaking.html", story=story, evidence=evidence, sources=[art], review=True,
                   decision_reason="review mode", next_step="Check.", published=art["published_at"],
                   alert_id="alert:1:v0:high")
    assert "Rain &amp; floods &lt;Hyderabad&gt;" in alert and 'href="https://ex.com/a?b=1&amp;c=2"' in alert
    digest = render("telegram_digest.html", date_label="2026-11-20", total=1, sources_count=1, light_news=False,
                    follow_ups=[], sections=[{"title": "Politics & Government",
                                              "items": [{"story": story, "sources": [art]}]}],
                    brief_id="digest:2026-11-20")
    assert "POLITICS &amp; GOVERNMENT" in digest and len(split_message(digest)) == 1


def test_section_heading_never_ends_a_message():
    text = "intro\n\n" + "\n\n".join(f"<b>━━ S{i} ━━</b>\n\n" + "<b>story</b> " + "z" * 300 for i in range(8))
    for part, _ in split_message(text, limit=700):
        assert not part.rstrip().endswith("━━</b>")
