"""Word matching shared by recall and search so both tolerate inflections.

Both languages the assistant speaks inflect, and the two sides of a memory rarely
agree on the form: a fact stored as "люблю пиццу" has to be findable by the question
"что я люблю?", and "Re:Zero" has to answer to "rezero". Plain substring matching fails
on both, so every query term is compared word by word against the stored text using a
prefix rule plus a similarity floor. This lives in core because two modules need it and
modules may not import each other's internals.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

WORD_PATTERN = re.compile(r"[\w]+", re.UNICODE)
SEPARATORS = re.compile(r"[\s\-_/:.,!?()\[\]«»\"']+")
MATCH_FLOOR = 0.85


def normalize_search_text(value: str | None) -> str:
    """Casefold, fold "ё" to "е" and reduce punctuation to single spaces."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(WORD_PATTERN.findall(normalized))


def word_similarity(token: str, word: str) -> float:
    """Score how confidently a query token stands for a stored word.

    Exact equality scores 1.0, a shared prefix of three letters or more scores high
    (which covers most Russian case endings), and anything else needs both words to be
    at least four letters long, to agree on their first four and to be similar overall.
    """
    if not token or not word:
        return 0.0
    if token == word:
        return 1.0
    token_length = len(token)
    word_length = len(word)
    if token_length >= 3 and word_length >= 3:
        if token.startswith(word) or word.startswith(token):
            return max(MATCH_FLOOR, 0.95 - (abs(token_length - word_length) * 0.05))
        if (
            token_length >= 4
            and word_length >= 4
            and token[:4] == word[:4]
            and SequenceMatcher(None, token, word).ratio() >= 0.7
        ):
            return MATCH_FLOOR
    return 0.0


def query_terms(query: str | None, *, minimum: int = 3, limit: int = 6) -> list[str]:
    """Normalized query words worth matching on; short filler words carry no meaning."""
    return [term for term in normalize_search_text(query).split() if len(term) >= minimum][:limit]


def term_in_text(term: str, text: str) -> bool:
    """True when a query term plausibly occurs in the text.

    Separators are dropped on both sides first so "rezero" still finds "Re:Zero",
    and single words are compared with the tolerant rule above.
    """
    if not term:
        return False
    haystack = normalize_search_text(text)
    if not haystack:
        return False
    if SEPARATORS.sub("", term) in SEPARATORS.sub("", haystack):
        return True
    return any(word_similarity(term, word) >= MATCH_FLOOR for word in haystack.split())


def text_matches_terms(text: str | None, terms: list[str], *, require_all: bool = False) -> bool:
    """Match stored text against query terms.

    Recall defaults to "any term": a question is a paraphrase, and demanding every word
    would drop the memory whenever the assistant words it slightly differently.
    """
    if not terms:
        return True
    checks = [term_in_text(term, text or "") for term in terms]
    return all(checks) if require_all else any(checks)
