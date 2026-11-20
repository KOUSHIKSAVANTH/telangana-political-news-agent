"""Political-only scope: sports/cinema/lifestyle dropped, political angles kept."""
from datetime import UTC, datetime

import pytest

from app.pipeline import heuristic_analysis
from app.relevance import Tagger
from app.rss_client import feed_tags
from app.settings import load_yaml
from app.verification import assess

TAGGER = Tagger(load_yaml("topics.yaml"))


@pytest.mark.parametrize("text,tags,url", [
    ("Hyderabad FC beat Bengaluru 2-1 at Gachibowli", ["Sport", "Football"], ""),
    ("Sunrisers retain three players ahead of IPL auction", [], ""),
    ("Box office: new Telugu film crosses Rs 100 crore", [], ""),
    ("Star cast attends pre-release event in Hyderabad", [], "https://www.deccanchronicle.com/entertainment/tollywood/x"),
    ("కొత్త సినిమా ట్రైలర్ విడుదల", [], ""),
    ("ఉప్పల్‌లో క్రికెట్ మ్యాచ్ టికెట్లు", [], ""),
])
def test_non_political_items_are_out_of_scope(text, tags, url):
    assert TAGGER.out_of_scope(text, tags=tags, url=url)


@pytest.mark.parametrize("text,tags", [
    ("Actor joins BJP ahead of GHMC elections", ["Entertainment"]),
    ("Government revises cinema ticket prices for big-budget films", []),
    ("Minister felicitates Hyderabad cricketer, announces Rs 1 crore award", ["Sport"]),
    ("సినిమా టికెట్ ధరలపై ప్రభుత్వ జీవో", []),
    ("Revanth Reddy reviews flood relief in Khammam", []),  # no exclusion signal at all
    ("Heavy rain lashes Hyderabad", []),
])
def test_political_or_neutral_items_stay(text, tags):
    assert TAGGER.out_of_scope(text, tags=tags) is None


def test_party_and_strike_words_do_not_rescue_cinema_or_sport():
    assert TAGGER.out_of_scope("Film team throws success party for actor")
    assert TAGGER.out_of_scope("Tilak Varma's strike rate lifts Hyderabad in T20 win")


def test_feed_tags_split_packed_terms():
    entry = {"tags": [{"term": "Sports,Cricket"}, {"term": "Cricket"}, {"term": " "}]}
    assert feed_tags(entry) == ["Sports", "Cricket"]


def ev(title, group):
    return {"id": hash(title) % 1000, "title": title, "title_en": None, "excerpt": "", "language": "en",
            "publisher": group, "independence_group": group, "source_type": "publisher",
            "published_at": datetime(2026, 11, 19, 4, tzinfo=UTC)}


def test_heuristic_marks_non_political_story_low():
    evidence = [ev("Fire breaks out at godown in Uppal; no casualties", "a")]
    a = heuristic_analysis(evidence, TAGGER, assess(evidence, copy_threshold=0.85, min_independent=2,
                                                       official_satisfies=True))
    assert a["priority"] == "low"


def test_heuristic_keeps_political_story():
    evidence = [ev("BRS stages dharna against power cuts in Siddipet", "a")]
    a = heuristic_analysis(evidence, TAGGER, assess(evidence, copy_threshold=0.85, min_independent=2,
                                                       official_satisfies=True))
    assert a["priority"] == "normal"
