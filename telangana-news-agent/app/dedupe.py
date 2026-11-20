"""URL canonicalization, text normalization and exact-duplicate hashing."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
    "cmp", "ito", "ncid", "src", "from", "amp", "utm",
}
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"), None)

EN_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "by", "with", "from", "as",
    "is", "are", "was", "were", "be", "been", "its", "it", "this", "that", "after", "over", "into",
    "amid", "says", "said", "say", "new", "will", "has", "have", "had", "not", "no", "his", "her",
    "their", "who", "what", "how", "why", "up", "out", "s", "vs", "against", "about", "per",
}


def canonicalize_url(url: str) -> str:
    """Stable URL form for de-duplication. Never invents a URL; only normalizes the given one."""
    url = url.strip()
    parts = urlsplit(url)
    scheme = "https" if parts.scheme in {"http", "https", ""} else parts.scheme
    host = parts.netloc.lower()
    if host.endswith(":443") or host.endswith(":80"):
        host = host.rsplit(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


def normalize_text(text: str) -> str:
    """NFKC, strip zero-width joiners, lowercase, replace punctuation/symbols with spaces.

    Keeps Telugu combining vowel signs (category M*), which naive \\w splitting would break.
    """
    text = unicodedata.normalize("NFKC", text or "").translate(ZERO_WIDTH).lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        out.append(" " if cat[0] in {"P", "S", "Z", "C"} else ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _stem(tok: str) -> str:
    if not tok.isascii():
        return tok
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)]
    return tok


def tokens(text: str) -> set[str]:
    return {_stem(t) for t in normalize_text(text).split() if len(t) > 1 and t not in EN_STOPWORDS}


def content_hash(title: str, group: str) -> str:
    """Exact-duplicate key: same normalized headline from the same publisher group."""
    return hashlib.sha256(f"{group}|{normalize_text(title)}".encode()).hexdigest()


def similarity(a: str | set[str], b: str | set[str]) -> float:
    """Dice coefficient over normalized, lightly stemmed tokens."""
    ta = a if isinstance(a, set) else tokens(a)
    tb = b if isinstance(b, set) else tokens(b)
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))
