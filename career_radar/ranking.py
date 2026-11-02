"""Profile-aware job ranking.

``Matcher(profile).apply(job)`` re-scores a stored job for the user's profile. The score starts from
the neutral base used by :mod:`career_radar.core` (40, "Canadian early-career opportunity") and adds
or subtracts points for the role level, interests, skills, preferred locations and excluded title
words. Only the first 6,000 characters of a description are scanned.

Speed: every profile term is compiled once. A job's text is tokenized once (ASCII bytes, C-level
``translate``/``split``) and an inverted index of the terms' first words picks the few terms whose
regexes are worth running, each tried only at occurrences of a literal needle. Term matching (skill
boundaries such as C / C++ / C# / Go / R and topic synonyms such as "embedded" -> firmware, RTOS,
FPGA) is shared with the resume heuristics in :mod:`career_radar.profile`.
"""
from __future__ import annotations

import functools
import re

from .core import fold
from .profile import (LEVELS, canonical_skill, interest_matchers, keys_present, normalize_profile, skill_matchers,
                      topic_key)

BASE_SCORE = 40
BASE_REASON = "Canadian early-career opportunity"
DESCRIPTION_CHARS = 6_000
LEVEL_LABELS = {"internship": "Internship", "co_op": "Co-op", "new_grad": "New grad", "entry_level": "Entry level"}
LEVEL_BONUS, LEVEL_PENALTY = 5, -20
INTEREST_POINTS, INTEREST_CAP = 15, 30              # interest in the title, company or tags
DESCRIPTION_POINTS, DESCRIPTION_CAP = 5, 10         # interest only in the description
SKILL_POINTS, SKILL_CAP, SKILLS_LISTED = 3, 21, 5
LOCATION_BONUS = 10
EXCLUDE_PENALTY = -30
PRIORITY_SCORE = 75

_PROVINCE_CODES = {"ontario": "on", "quebec": "qc", "british columbia": "bc", "alberta": "ab", "manitoba": "mb",
                   "saskatchewan": "sk", "nova scotia": "ns", "new brunswick": "nb",
                   "newfoundland and labrador": "nl", "newfoundland": "nl", "prince edward island": "pe",
                   "yukon": "yt", "northwest territories": "nt", "nunavut": "nu"}
# Same tokens as profile.letter_tokens: ASCII letters lower-cased, everything else a separator.
_LETTERS = bytes((c + 32) if 65 <= c <= 90 else (c if 97 <= c <= 122 else 32) for c in range(256))
_LOWER = bytes((c + 32) if 65 <= c <= 90 else c for c in range(256))


def _text(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


@functools.lru_cache(maxsize=8192)
def _folded(text: str) -> str:
    """Accent- and case-insensitive form; non-Latin text (which ``fold`` drops) is kept casefolded."""
    return fold(text) + "\n" + text.casefold()


@functools.lru_cache(maxsize=4096)
def _tag_keys(tag: str) -> tuple:
    return fold(tag).strip(), topic_key(tag)


def _phrase(term: str, *, plural: bool = False) -> str:
    """Regex for a folded phrase as whole words; spaces and hyphens inside it are flexible."""
    body = re.escape(term).replace(r"\ ", r"[\s,./&-]+").replace(r"\-", r"[\s-]?")
    left = r"(?<![a-z0-9])" if term[:1].isalnum() else ""
    suffix = "(?:s|es)?" if plural and term[-1:].isalpha() else ""
    right = r"(?![a-z0-9])" if term[-1:].isalnum() else ""
    return left + body + suffix + right


def _location_pattern(preference: str):
    """``(regex, tag)`` for a preferred location: "Ontario" also matches ", ON"; "Toronto, ON" matches Toronto."""
    folded = re.sub(r"\s+", " ", fold(preference) or preference.casefold()).strip(" ,")
    place = folded.split(",")[0].strip() or folded
    names = [name for name, code in _PROVINCE_CODES.items() if place in (name, code)]
    if names:
        code = _PROVINCE_CODES[names[0]]
        options = [_phrase(name) for name in names]
        options.append(r"(?:^|[,(/|]|\s)\s*" + code
                       + r"(?=\s*(?:$|[,;/|)(])|\s+[-–—]\s|\s+(?:or|and|&)\s|\s*,\s*canada\b)")
    else:
        options = [_phrase(place)]
    return re.compile("|".join(options), re.M), place


class _Scan:
    """A job text tokenized once. ``data`` has one byte per character, so its offsets are ``raw``'s."""

    __slots__ = ("raw", "data", "tokens", "_low", "_lower")

    def __init__(self, raw: str, vocabulary: frozenset):
        self.raw = raw
        self.data = raw.encode("ascii", "replace")
        # Only the words some profile term needs: no set of every word in a 6,000-character text.
        self.tokens = vocabulary.intersection(self.data.translate(_LETTERS).split())
        self._low = self._lower = None

    def matches(self, needle, matcher, direct: bool) -> bool:
        """Whether ``matcher`` matches, trying its regex only where its literal needle occurs."""
        raw, regex = self.raw, matcher[1]
        if direct:                        # the regex starts with its literal: the engine scans in C
            return regex.search(raw) is not None
        if needle is not None:            # ASCII needle: search the byte copy
            if matcher[2]:
                hay = self.data
            else:
                if self._low is None:
                    self._low = self.data.translate(_LOWER)
                hay = self._low
        else:                             # non-ASCII needle ("I²C"): search the text itself
            needle = matcher[0]
            if not needle:
                return regex.search(raw) is not None
            if matcher[2]:
                hay = raw
            else:
                if self._lower is None:
                    self._lower = raw.lower()
                hay = self._lower
                if len(hay) != len(raw):  # lower() moved offsets (rare Unicode)
                    return regex.search(raw) is not None
        index = hay.find(needle)
        while index >= 0:
            if regex.match(raw, index):
                return True
            index = hay.find(needle, index + 1)
        return False


class _Terms:
    """Named terms (each with one or more matchers) behind an inverted index of their first word."""

    def __init__(self, entries: list):
        self.names = [entry[0] for entry in entries]
        self.extra = [entry[2] if len(entry) > 2 else None for entry in entries]
        self.matchers, self.always, index, vocabulary = [], [], {}, set()
        for position, entry in enumerate(entries):
            for matcher in entry[1]:
                needle, regex, sensitive, keys = matcher[0], matcher[1], matcher[2], matcher[3]
                direct = bool(sensitive and needle and regex.pattern.startswith(re.escape(needle)))
                ascii_needle = needle.encode("ascii") if needle and needle.isascii() else None
                slot = len(self.matchers)
                self.matchers.append((position, ascii_needle, matcher, direct))
                if keys is None:
                    self.always.append(slot)
                    continue
                for token in keys[0]:
                    index.setdefault(token, []).append(slot)
                for options in keys:
                    vocabulary.update(options)
        self.index = index
        self.vocabulary = frozenset(vocabulary)

    def __bool__(self) -> bool:
        return bool(self.names)

    def found(self, scan: _Scan, skip=()) -> list:
        """Positions (in profile order) of the terms matching ``scan``, except those in ``skip``."""
        tokens, index = scan.tokens, self.index
        slots = set(self.always)
        for token in tokens:
            hit = index.get(token)
            if hit:
                slots.update(hit)
        if not slots:
            return []
        done, hits = set(skip), []
        for slot in sorted(slots):
            position, needle, matcher, direct = self.matchers[slot]
            if position in done:
                continue
            keys = matcher[3]
            if keys is not None and len(keys) > 1 and not keys_present(keys, tokens):
                continue
            if scan.matches(needle, matcher, direct):
                done.add(position)
                hits.append(position)
        return sorted(hits)


class Matcher:
    """Scores jobs for one candidate profile; all patterns are compiled once in ``__init__``."""

    def __init__(self, profile: dict | None):
        self.profile = normalize_profile(profile if isinstance(profile, dict) else None)
        p = self.profile
        self.levels = tuple(p["levels"])
        self._level_set = frozenset(p["levels"])
        self.terms = list(p["interests"])
        self.active = bool(p["interests"] or p["skills"] or p["preferred_locations"] or p["exclude_keywords"]
                           or self._level_set != frozenset(LEVELS))
        interests = []
        for interest in p["interests"]:
            tag_keys = frozenset({fold(interest).strip(), topic_key(interest)} - {""})
            interests.append((interest, tuple(interest_matchers(interest)), tag_keys))
        self._interests = _Terms(interests)
        skills, seen = [], set()
        for skill in p["skills"]:
            key = canonical_skill(skill).casefold()
            matchers = tuple(skill_matchers(skill))
            if matchers and key not in seen:
                seen.add(key)
                skills.append((skill, matchers))
        self._skills = _Terms(skills)
        self._vocabulary = self._interests.vocabulary | self._skills.vocabulary
        self._locations = [(pref, *_location_pattern(pref)) for pref in p["preferred_locations"]
                           if (fold(pref) or pref.casefold()).strip(" ,")]
        self._excludes = []
        for keyword in p["exclude_keywords"]:
            folded = re.sub(r"\s+", " ", fold(keyword) or keyword.casefold()).strip()
            if folded:
                self._excludes.append((keyword, re.compile(_phrase(folded, plural=True))))

    @staticmethod
    def _passthrough(job: dict) -> dict:
        score = job.get("priority_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            score = BASE_SCORE
        reasons = job.get("priority_reasons")
        reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, (list, tuple)) else [BASE_REASON]
        return {"score": int(score), "reasons": reasons, "terms": [], "priority": False}

    def score(self, job: dict) -> dict:
        """``{'score': 0-100, 'reasons': [...], 'terms': [matched interests/skills], 'priority': bool}``."""
        if not self.active:
            return self._passthrough(job)
        title, company = _text(job.get("title")), _text(job.get("company"))
        raw_tags = job.get("tags")
        tags = [tag for tag in raw_tags if isinstance(tag, str)] if isinstance(raw_tags, (list, tuple)) else []
        score, reasons = BASE_SCORE, [BASE_REASON]

        level = job.get("level")
        if level in self._level_set:
            score += LEVEL_BONUS
            reasons.append(f"Role level you want: {LEVEL_LABELS[level]}")
        elif level in LEVEL_LABELS:
            score += LEVEL_PENALTY
            reasons.append("Not one of your preferred role levels")

        tag_keys = {key for tag in tags for key in _tag_keys(tag)}
        strong, weak, skills = [], [], []
        interests, body = self._interests, None
        if interests or self._skills:
            body = _Scan(title + "\n" + _text(job.get("description"))[:DESCRIPTION_CHARS], self._vocabulary)
        if interests:
            by_tag = [i for i, keys in enumerate(interests.extra) if keys & tag_keys]
            head = _Scan(f"{title}\n{company}\n{' '.join(tags)}", self._vocabulary)
            strong = sorted(by_tag + interests.found(head, skip=by_tag))
            if len(strong) < len(interests.names):
                weak = interests.found(body, skip=strong)
            strong = [interests.names[i] for i in strong]
            weak = [interests.names[i] for i in weak]
        if strong:
            score += min(INTEREST_POINTS * len(strong), INTEREST_CAP)
            reasons += [f"Matches your interest: {interest}" for interest in strong]
        if weak:
            score += min(DESCRIPTION_POINTS * len(weak), DESCRIPTION_CAP)
            plural = "s" if len(weak) > 1 else ""
            reasons.append(f"Description mentions your interest{plural}: {', '.join(weak[:3])}")

        if self._skills:
            skills = [self._skills.names[i] for i in self._skills.found(body)]
        if skills:
            score += min(SKILL_POINTS * len(skills), SKILL_CAP)
            reasons.append(f"{len(skills)} of your skills: {', '.join(skills[:SKILLS_LISTED])}")

        if self._locations:
            where = _folded(_text(job.get("location"))[:1000])
            for preference, regex, tag in self._locations:
                if regex.search(where) or tag in tag_keys:
                    score += LOCATION_BONUS
                    reasons.append(f"In a location you prefer: {preference}")
                    break

        excluded = None
        if self._excludes:
            folded_title = _folded(title[:1000])
            excluded = next((keyword for keyword, regex in self._excludes if regex.search(folded_title)), None)
            if excluded:
                score += EXCLUDE_PENALTY
                reasons.append(f"Title contains an excluded word: {excluded}")

        score = max(0, min(100, int(score)))
        terms, seen = [], set()
        for term in strong + weak + skills:
            if term.casefold() not in seen:
                seen.add(term.casefold())
                terms.append(term)
        priority = bool((strong or score >= PRIORITY_SCORE) and not excluded)
        return {"score": score, "reasons": reasons, "terms": terms, "priority": priority}

    def apply(self, job: dict) -> dict:
        """A new job dict with the profile score in ``priority_score``/``priority_reasons`` and ``match``."""
        result = self.score(job)
        if not self.active:
            return job | {"match": result}
        return job | {"priority_score": result["score"], "priority_reasons": list(result["reasons"]), "match": result}
