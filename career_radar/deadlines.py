"""Pure parsing of explicitly published application-deadline clauses.

Values are complete ISO calendar dates or offset-aware ISO instants. A regional
wall clock is an instant only when its UTC round trip is valid and unique; no
posting date, current year, default timezone, or end-of-day time is supplied.
Evidence is an unmodified slice of the source, never decorated with caveats.

Stable uncertainty values (None means no unresolved published clock):
* missing_timezone: a clock was published without a zone, including RBC's note.
* unknown_timezone: an unknown/malformed zone or offset, including RFC 3339 -00:00.
* invalid_clock: an invalid or unsupported clock, never a truncated valid prefix.
* ambiguous_local_time: a regional DST fold admits more than one instant.
* nonexistent_local_time: a regional DST gap admits no instant.

Invalid clocks take precedence over timezone uncertainty. A date-only clause
without a clock has uncertainty=None; it does not imply any particular cutoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["PublishedDeadline", "parse_published_deadline", "parse_structured_deadline", "explicit_deadline"]


@dataclass(frozen=True)
class PublishedDeadline:
    value: str
    evidence: str
    uncertainty: str | None = None


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)
_MONTHS = {spelling.lower(): number for number, name in enumerate(_MONTH_NAMES, 1)
           for spelling in (name, name[:3])}
_MONTHS["sept"] = 9
_MONTH = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"[0-9]{1,2}(?:st|nd|rd|th)?"
_DATE = (rf"(?P<date>[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}|"
         rf"{_MONTH}\s+{_DAY},?\s+[0-9]{{4}}|{_DAY}\s+{_MONTH},?\s+[0-9]{{4}})(?![0-9])")
_SEPARATOR = r"\s*(?:(?:is|of|on|at)\b|[:—-])?\s*"
_LABEL = (r"\b(?:apply\s+by|applications?\s+(?:deadline|closing\s+date|closes?(?:\s+(?:on|at))?)|"
          r"closing\s+date|deadline(?:\s+to\s+apply)?|date\s+limite|advertised\s+until)\b")
# RBC publishes next-day metadata; its formal prose is more authoritative.
_DEADLINE_PATTERNS = (
    re.compile(r"\bformal\s+application\s+deadline\b" + _SEPARATOR + _DATE, re.I),
    re.compile(_LABEL + _SEPARATOR + _DATE, re.I),
)

# Capture whole clock-shaped tokens first, then validate them. Restrictive
# optional regex groups can silently accept the valid prefix of a bad clock.
_DEADLINE_CLOCK = re.compile(
    r"(?:T|\s*(?:(?:at|by|à)\b|,)\s*|\s+)"
    r"(?P<clock>[+-]?[0-9]+(?::[0-9]*)*(?:\.[0-9]+)*"
    r"(?:\s*(?:AM|PM)\b)?|midnight\b|noon\b)", re.I,
)
_CLOCK_FIELDS = re.compile(
    r"(?P<hour>[0-9]{1,2})(?::(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2})(?:\.(?P<fraction>[0-9]{1,6}))?)?)?"
    r"(?:\s*(?P<ampm>AM|PM))?", re.I,
)
# Unknown tokens are retained rather than falling back to a recognized prefix.
_DEADLINE_ZONE = re.compile(
    r"\s*(?P<zone>\([^()\r\n]*\)?|(?:UTC|GMT)[ \t]+[+-][^\s,;!?()]+|"
    r"[A-Za-z+-][^\s,;!?()]*)", re.I,
)
_OFFSET = re.compile(r"(?:UTC|GMT)?\s*([+-])([0-9]{2}):?([0-9]{2})", re.I)
_REGIONAL = {
    "ET": "America/Toronto", "CT": "America/Winnipeg", "MT": "America/Edmonton",
    "PT": "America/Vancouver", "AT": "America/Halifax", "NT": "America/St_Johns",
}
_FIXED_MINUTES = {
    "Z": 0, "UTC": 0, "GMT": 0, "EST": -300, "EDT": -240, "CST": -360,
    "CDT": -300, "MST": -420, "MDT": -360, "PST": -480, "PDT": -420,
    "AST": -240, "ADT": -180, "NST": -210, "NDT": -150,
}
_CLAUSE_END = re.compile(r"\.(?=\s|$)|[;!?\r\n]")
_RBC_LABEL = re.compile(r"Application\s+Deadline\b", re.I)
_RBC_NOTE = re.compile(
    r"Note:\s*Applications will be accepted until 11:59 PM on the day prior "
    r"to the application deadline date above\.?", re.I,
)


def _calendar_date(value: str) -> str | None:
    """Require a full, valid date; English month parsing is locale-independent."""
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value).isoformat()
        parts = value.replace(",", "").replace(".", "").lower().split()
        if parts[0] in _MONTHS:
            month, day, year = _MONTHS[parts[0]], parts[1], parts[2]
        else:
            day, month, year = parts[0], _MONTHS[parts[1]], parts[2]
        day = re.sub(r"(?:st|nd|rd|th)$", "", day)
        return date(int(year), month, int(day)).isoformat()
    except (ValueError, KeyError, IndexError):
        return None


def _read_zone(text: str, start: int) -> tuple[str | None, int]:
    match = _DEADLINE_ZONE.match(text, start)
    if not match:
        return None, start
    token = match.group("zone")
    # A sentence period is evidence punctuation, not part of a timezone name.
    trimmed = token.rstrip(".")
    end = match.end() - (len(token) - len(trimmed))
    if trimmed.startswith("(") and trimmed.endswith(")"):
        if end < len(text) and text[end] not in " \t\r\n.,;!?":
            return trimmed, end  # Malformed attached suffix: do not use its prefix.
        trimmed = trimmed[1:-1].strip()
    return trimmed, end


def _published_instant(day: str, clock: str, zone: str | None) -> tuple[str | None, str | None]:
    """Return an exact instant or a stable reason for retaining only its date."""
    fraction = None
    if clock.lower() in {"midnight", "noon"}:
        hour, minute, second, microsecond = (0 if clock.lower() == "midnight" else 12), 0, 0, 0
    else:
        fields = _CLOCK_FIELDS.fullmatch(clock)
        if not fields or (fields["minute"] is None and fields["ampm"] is None):
            return None, "invalid_clock"
        hour = int(fields["hour"])
        if fields["ampm"]:
            if not 1 <= hour <= 12:
                return None, "invalid_clock"
            hour = hour % 12 + (12 if fields["ampm"].upper() == "PM" else 0)
        minute, second = int(fields["minute"] or 0), int(fields["second"] or 0)
        fraction = fields["fraction"]
        microsecond = int((fraction or "0").ljust(6, "0"))
    try:
        naive = datetime.fromisoformat(day).replace(
            hour=hour, minute=minute, second=second, microsecond=microsecond,
        )
    except ValueError:
        return None, "invalid_clock"
    if not zone:
        return None, "missing_timezone"
    offset = _OFFSET.fullmatch(zone)
    try:
        if offset:
            hours, minutes = int(offset[2]), int(offset[3])
            if hours > 23 or minutes > 59 or (offset[1] == "-" and hours == minutes == 0):
                return None, "unknown_timezone"
            tz = timezone(timedelta(minutes=(hours * 60 + minutes) * (-1 if offset[1] == "-" else 1)))
        elif zone.upper() in _FIXED_MINUTES:
            tz = timezone(timedelta(minutes=_FIXED_MINUTES[zone.upper()]))
        elif zone.upper() in _REGIONAL or "/" in zone:
            tz = ZoneInfo(_REGIONAL.get(zone.upper(), zone))
        else:
            return None, "unknown_timezone"
    except (ValueError, ZoneInfoNotFoundError):
        return None, "unknown_timezone"

    aware = naive.replace(tzinfo=tz)
    if isinstance(tz, ZoneInfo):
        # A gap has no valid round trip. A fold has two distinct UTC offsets.
        # Test both folds rather than arbitrarily choosing fold=0 or fold=1.
        valid = []
        try:
            for fold in (0, 1):
                candidate = naive.replace(tzinfo=tz, fold=fold)
                restored = candidate.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
                if restored == naive:
                    valid.append(candidate)
        except (ValueError, OverflowError):
            return None, "invalid_clock"
        if not valid:
            return None, "nonexistent_local_time"
        if len({candidate.utcoffset() for candidate in valid}) > 1:
            return None, "ambiguous_local_time"
        aware = valid[0]
    return aware.isoformat(timespec="microseconds" if fraction is not None else "seconds"), None


def _clause_end(text: str, start: int) -> int:
    match = _CLAUSE_END.search(text, start)
    if not match:
        return len(text.rstrip())
    return match.start() if match.group() in "\r\n" else match.end()


def parse_published_deadline(text: str) -> PublishedDeadline | None:
    """Parse the first valid labelled date, preferring a formal prose deadline.

    Exact instants preserve published seconds, microseconds and numeric offsets.
    Invalid/uncertain clocks keep their calendar date and original evidence.
    Midnight means 00:00 of the explicit day, not an inferred following day.
    RBC's explicit prior-day note overrides displayed metadata, but its unzoned
    11:59 PM remains date-only (missing_timezone), even if metadata had a zone.
    """
    for pattern in _DEADLINE_PATTERNS:
        for match in pattern.finditer(text):
            day = _calendar_date(match["date"])
            if not day:
                continue
            value, uncertainty, end = day, None, match.end()
            clock = _DEADLINE_CLOCK.match(text, end)
            if clock:
                zone, end = _read_zone(text, clock.end())
                instant, uncertainty = _published_instant(day, clock["clock"], zone)
                value = instant or day
            if _RBC_LABEL.match(text, match.start()):
                prior = _RBC_NOTE.search(text, end, end + 280)
                # Do not associate the note with a different intervening date.
                intervening = prior and any(p.search(text, end, prior.start()) for p in _DEADLINE_PATTERNS)
                if prior and not intervening:
                    try:
                        value = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
                    except OverflowError:
                        return None
                    return PublishedDeadline(value, text[match.start():prior.end()], "missing_timezone")
            return PublishedDeadline(value, text[match.start():_clause_end(text, end)], uncertainty)
    return None


def parse_structured_deadline(value: object) -> PublishedDeadline | None:
    """Validate identified ISO-like fields without a lossy datetime round trip."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:$|T| )", text) or not _calendar_date(text[:10]):
        return None
    parsed = parse_published_deadline("Deadline: " + text)
    if parsed is None:
        return None
    uncertainty = parsed.uncertainty
    if len(text) > 10 and "T" not in parsed.value and uncertainty is None:
        uncertainty = "invalid_clock"
    return PublishedDeadline(parsed.value, value, uncertainty)


def explicit_deadline(text: str) -> tuple[str | None, str | None]:
    """Compatibility surface: no result is (None, None); evidence stays verbatim."""
    parsed = parse_published_deadline(text)
    return (parsed.value, parsed.evidence) if parsed else (None, None)
