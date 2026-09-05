"""
Language segmentation for code-switched TTS.

Splits LLM output text into clause-level (Hindi, English) spans so each span
can be routed to a matching Rime voice before stitching. Deliberately segments
at clause boundaries (punctuation, conjunctions) rather than word-by-word:
word-level switching produces choppy, obviously-stitched audio, while
clause-level switching both sounds more natural and matches how real
code-switchers actually speak.

Detection strategy (cheap, no external LID model required for the hackathon
timeline):
  1. Devanagari-script text -> unambiguously Hindi.
  2. Latin-script text -> checked against a small romanized-Hindi function-word
     list (the words that carry the most signal: "hai", "kya", "nahi", "kaise",
     "bhai", "yaar", "kitna", "kab", "abhi", etc.). A clause with a majority of
     these hits is tagged Hindi-in-Latin-script; otherwise English.

This is a heuristic, not a trained classifier -- it will misclassify borrowed
words and proper nouns sometimes. Log every segmentation decision so the
regression harness (Component "evidence generator") can score classification
accuracy against a labeled test set later.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("language-segmenter")

# Devanagari Unicode block
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

# Small, high-signal set of romanized Hindi function words / discourse markers.
# Deliberately not exhaustive -- these are the words most likely to appear
# regardless of topic, which makes them a decent per-clause signal even in a
# short clause. Extend this list from real transcripts as you collect them.
_HINDI_LATIN_MARKERS = {
    "bhai", "yaar", "kya", "kaise", "kaisi", "kaisa", "hai", "hain", "tha",
    "thi", "the", "nahi", "nahin", "haan", "kab", "kahan", "kitna", "kitni",
    "kyun", "kyu", "mujhe", "tumhe", "aapko", "abhi", "accha", "theek",
    "arre", "arrey", "matlab", "bas", "chalo", "toh", "ki", "ka", "ke",
    "ko", "se", "aur", "par", "lekin", "magar",
}

_CLAUSE_SPLIT_RE = re.compile(r"([,.!?;]|\band\b|\bbut\b)", re.IGNORECASE)


class Lang(str, Enum):
    HINDI = "hi"
    ENGLISH = "en"


@dataclass
class Segment:
    text: str
    lang: Lang


def _classify_clause(clause: str) -> Lang:
    clause = clause.strip()
    if not clause:
        return Lang.ENGLISH

    if _DEVANAGARI_RE.search(clause):
        return Lang.HINDI

    words = re.findall(r"[a-zA-Z']+", clause.lower())
    if not words:
        return Lang.ENGLISH

    hindi_hits = sum(1 for w in words if w in _HINDI_LATIN_MARKERS)
    ratio = hindi_hits / len(words)

    # Threshold is deliberately low: a short clause like "bhai kaise ho" is
    # 100% marker words, but "what is the cancellation charge bhai" is ~14%
    # and should still tip Hindi given "bhai" is a strong per-clause signal.
    is_hindi = ratio >= 0.2 or (hindi_hits >= 1 and len(words) <= 4)
    return Lang.HINDI if is_hindi else Lang.ENGLISH


def segment_text(text: str) -> list[Segment]:
    """Split text into clause-level (text, lang) segments, merging adjacent
    clauses that share a language so we don't over-fragment the TTS calls."""
    raw_clauses = [c for c in _CLAUSE_SPLIT_RE.split(text) if c.strip()]

    # Re-attach split delimiters to the clause they follow so punctuation
    # stays with its sentence for correct TTS prosody.
    clauses: list[str] = []
    buf = ""
    for piece in raw_clauses:
        if _CLAUSE_SPLIT_RE.fullmatch(piece.strip()) and buf:
            clauses.append((buf + piece).strip())
            buf = ""
        else:
            buf += piece
    if buf.strip():
        clauses.append(buf.strip())

    segments: list[Segment] = []
    for clause in clauses:
        lang = _classify_clause(clause)
        if segments and segments[-1].lang == lang:
            segments[-1] = Segment(text=segments[-1].text + " " + clause, lang=lang)
        else:
            segments.append(Segment(text=clause, lang=lang))

    logger.info(
        "Segmented into %d span(s): %s",
        len(segments),
        [(s.lang.value, s.text) for s in segments],
    )
    return segments