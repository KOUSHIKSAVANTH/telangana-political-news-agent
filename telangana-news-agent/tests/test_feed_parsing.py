from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.rss_client import clean_excerpt, fetch_source, parse_feed

FIXTURE = (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_bytes()
SOURCE = {"id": "sample", "language": "en", "feed_url": "https://example.com/feed"}
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def parse():
    return parse_feed(FIXTURE, SOURCE, max_age=timedelta(hours=72), excerpt_chars=400, now=NOW)


def test_parses_valid_items_and_skips_bad_ones():
    titles = [i.title for i in parse()]
    assert "Heavy rain lashes Hyderabad; GHMC issues advisory" in titles
    assert "Old archive story" not in titles  # older than max age
    assert "Item with no link" not in titles


def test_publisher_timestamp_converted_to_utc():
    item = next(i for i in parse() if "GHMC" in i.title)
    assert item.published_at == datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def test_future_timestamp_is_not_trusted():
    item = next(i for i in parse() if "future" in i.title)
    assert item.published_at is None


def test_url_canonicalized_but_original_link_kept():
    item = next(i for i in parse() if "GHMC" in i.title)
    assert item.url == "https://example.com/news/heavy-rain-hyderabad"
    assert item.raw_metadata["link"].startswith("https://www.example.com/news/heavy-rain-hyderabad/?utm_source")


def test_html_stripped_from_excerpt():
    item = next(i for i in parse() if "GHMC" in i.title)
    assert "<" not in item.excerpt and "Hyderabad on Tuesday" in item.excerpt


def test_telugu_preserved():
    item = next(i for i in parse() if i.url.endswith("rain-alert"))
    assert "తెలంగాణ" in item.title and "ఆరెంజ్ అలర్ట్" in item.title


def test_clean_excerpt_truncates_on_word_boundary():
    out = clean_excerpt("word " * 200, 50)
    assert len(out) <= 51 and out.endswith("…")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_conditional_get_304():
    def handler(req):
        assert req.headers["If-None-Match"] == '"abc"'
        return httpx.Response(304)

    res = fetch_source(_client(handler), SOURCE, etag='"abc"', last_modified=None,
                       max_age=timedelta(hours=72), excerpt_chars=400)
    assert res.ok and res.not_modified and res.items == []


def test_feed_error_is_isolated():
    res = fetch_source(_client(lambda req: httpx.Response(500)), SOURCE, etag=None, last_modified=None,
                       max_age=timedelta(hours=72), excerpt_chars=400)
    assert not res.ok and "500" in res.error


def test_html_page_instead_of_feed_is_an_error():
    res = fetch_source(_client(lambda req: httpx.Response(200, text="<html><body>Not a feed</body></html>")),
                       SOURCE, etag=None, last_modified=None, max_age=timedelta(hours=72), excerpt_chars=400)
    assert not res.ok
