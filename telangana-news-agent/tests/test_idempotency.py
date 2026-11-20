from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.database import Database
from app.email_sender import EmailSender, RecipientNotAllowed, SendResult
from app.models import FeedItem
from app.pipeline import deliver
from app.settings import get_settings


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 't.db'}")
    d.create_all()
    d.sync_sources([{"id": "src", "name": "Src", "language": "en", "type": "publisher", "enabled": True,
                     "independence_group": "src", "feed_url": "https://example.com/feed"}])
    return d


@pytest.fixture
def settings(tmp_path):
    return replace(get_settings(), dry_run=False, output_dir=tmp_path / "out", email_sender="bot@example.com",
                   team_recipients=["team@example.com"], recipient_allowlist=["team@example.com"])


class CountingSender:
    def __init__(self, fail_times=0):
        self.calls, self.fail_times = 0, fail_times

    def send(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("smtp down")
        return SendResult("sent", f"msg-{self.calls}")


def _deliver(settings, db, sender, key="digest:2026-11-19"):
    return deliver(settings, db, sender, key=key, type_="digest", recipient_group="team",
                   recipients=settings.team_recipients, subject="s", html="<p>h</p>", text="t", story_id=None)


def test_same_key_sends_once(settings, db):
    sender = CountingSender()
    assert _deliver(settings, db, sender) is not None
    assert _deliver(settings, db, sender) is None
    assert sender.calls == 1
    assert db.get_notification("digest:2026-11-19")["status"] == "sent"


def test_failed_send_can_be_retried_then_never_again(settings, db):
    sender = CountingSender(fail_times=1)
    with pytest.raises(RuntimeError):
        _deliver(settings, db, sender)
    assert db.get_notification("digest:2026-11-19")["status"] == "failed"
    assert _deliver(settings, db, sender) is not None
    assert _deliver(settings, db, sender) is None
    assert sender.calls == 2


def test_dry_run_keys_do_not_block_live_send(settings, db):
    sender = CountingSender()
    _deliver(replace(settings, dry_run=True), db, sender)
    assert _deliver(settings, db, sender) is not None
    assert sender.calls == 2


def test_duplicate_article_not_inserted_twice(db):
    item = FeedItem(source_id="src", title="Title", url="https://example.com/a", excerpt="x",
                    published_at=datetime(2026, 11, 18, tzinfo=UTC))
    assert db.insert_article(item, content_hash="h1", relevant=True) is not None
    assert db.insert_article(item, content_hash="h2", relevant=True) is None  # same URL
    other = item.model_copy(update={"url": "https://example.com/b"})
    assert db.insert_article(other, content_hash="h1", relevant=True) is None  # same content


def test_live_send_requires_allowlist(settings):
    s = EmailSender(replace(settings, recipient_allowlist=[]))
    with pytest.raises(RecipientNotAllowed):
        s.send(recipients=["team@example.com"], subject="s", html="h", text="t", idempotency_key="k")
    s2 = EmailSender(settings)
    with pytest.raises(RecipientNotAllowed):
        s2.send(recipients=["stranger@example.com"], subject="s", html="h", text="t", idempotency_key="k")


def test_dry_run_writes_preview_and_sends_nothing(settings, tmp_path):
    res = EmailSender(replace(settings, dry_run=True)).send(
        recipients=[], subject="s", html="<p>h</p>", text="t", idempotency_key="dryrun:digest:x")
    assert res.status == "dry_run" and (tmp_path / "out" / "dryrun_digest_x.html").exists()


def test_api_usage_counter(db):
    db.add_usage("tavily", 1)
    db.add_usage("tavily", 2)
    assert db.get_usage("tavily") == 3
