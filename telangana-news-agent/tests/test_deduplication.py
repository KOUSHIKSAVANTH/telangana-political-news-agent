from datetime import UTC, datetime, timedelta

from app.cluster import Candidate, best_match, match_tokens
from app.dedupe import canonicalize_url, content_hash, normalize_text
from app.models import Verification
from app.verification import assess, cap_status

T0 = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)


def test_canonicalize_strips_tracking_fragment_and_www():
    assert canonicalize_url("http://WWW.Example.com/a/b/?utm_source=x&id=5&fbclid=1#top") == \
        "https://example.com/a/b?id=5"


def test_canonicalize_is_idempotent():
    u = "https://example.com/a?b=2&a=1"
    assert canonicalize_url(canonicalize_url(u)) == canonicalize_url(u)


def test_content_hash_ignores_punctuation_and_case_within_publisher():
    assert content_hash("KTR slams govt!", "thehindu") == content_hash("ktr slams govt", "thehindu")
    assert content_hash("KTR slams govt", "thehindu") != content_hash("KTR slams govt", "siasat")


def test_telugu_normalization_removes_zero_width_joiner_but_keeps_vowel_signs():
    assert normalize_text("తెలంగాణ‌లో") == "తెలంగాణలో"
    assert "ి" in normalize_text("విద్యార్థి")  # combining vowel sign kept


def _cands(*titles):
    return [Candidate(i, match_tokens(t), T0) for i, t in enumerate(titles)]


def test_same_event_from_different_outlets_clusters():
    cands = _cands("Kothagudem police seize 280 BGL shells in forest dumps")
    sid = best_match("Three Maoist dumps containing 280 BGL shells recovered in forest area",
                     T0 + timedelta(hours=2), cands, threshold=0.42, window=timedelta(hours=36))
    assert sid == 0


def test_generic_words_alone_do_not_cluster():
    cands = _cands("Six arrested for gambling in Hyderabad, Rs 9.3 lakh seized")
    sid = best_match("Six arrested for illegal LPG cylinder refilling in Hyderabad",
                     T0, cands, threshold=0.42, window=timedelta(hours=36))
    assert sid is None


def test_outside_time_window_does_not_cluster():
    cands = _cands("FSSAI licences of four restaurants suspended over food safety violations")
    sid = best_match("FSSAI licences of four restaurants suspended over food safety violations",
                     T0 + timedelta(days=5), cands, threshold=0.42, window=timedelta(hours=36))
    assert sid is None


def _ev(group, title, excerpt="", source_type="publisher"):
    return {"independence_group": group, "publisher": group.title(), "source_type": source_type,
            "title": title, "excerpt": excerpt}


def test_copied_wire_report_counts_once():
    text = "Cabinet approves new irrigation project worth Rs 5,000 crore in Nalgonda district"
    s = assess([_ev("a", text, "PTI report body"), _ev("b", text, "PTI report body"),
                _ev("c", "Rs 5,000 crore irrigation nod for Nalgonda", "Different reporting")])
    assert s.independent_sources == 2 and s.copies_collapsed == 1
    assert s.max_status == Verification.confirmed


def test_same_publisher_two_feeds_is_one_source():
    s = assess([_ev("thehindu", "Story one"), _ev("thehindu", "Story one, Hyderabad edition")])
    assert s.independent_sources == 1 and s.max_status == Verification.developing


def test_official_source_satisfies_threshold():
    s = assess([_ev("govt", "Government announces holiday", source_type="official")])
    assert s.max_status == Verification.confirmed
    s2 = assess([_ev("govt", "Government announces holiday", source_type="official")], official_satisfies=False)
    assert s2.max_status == Verification.developing


def test_model_cannot_claim_more_than_evidence_supports():
    assert cap_status("confirmed", Verification.developing) == Verification.developing
    assert cap_status("unverified", Verification.confirmed) == Verification.unverified
