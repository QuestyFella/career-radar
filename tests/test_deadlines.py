"""Pure-parser contracts; these tests deliberately do not import core or extras."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import ast
import inspect

import pytest

from career_radar import deadlines
from career_radar.deadlines import PublishedDeadline, explicit_deadline, parse_published_deadline


@pytest.mark.parametrize("text,value,uncertainty", [
    ("Application Deadline: November 1, 2026 at 1:30 AM ET.",
     "2026-11-01", "ambiguous_local_time"),
    ("Application Deadline: March 8, 2026 at 2:30 AM ET.",
     "2026-03-08", "nonexistent_local_time"),
    ("Application Deadline: November 5, 2026 at 17:30:15 UTC.",
     "2026-11-05T17:30:15+00:00", None),
    ("Application Deadline: November 5, 2026 at 17:30 -05:00.",
     "2026-11-05T17:30:00-05:00", None),
], ids=["review-dst-fold", "review-dst-gap", "review-seconds", "review-numeric-offset"])
def test_review_regressions(text, value, uncertainty):
    got = parse_published_deadline(text)
    assert got is not None
    assert got.value == value
    assert got.evidence == text
    assert got.uncertainty == uncertainty


# Positive fixtures copied from the existing extras tests, without running any adapter.
@pytest.mark.parametrize("text,value", [
    ("Application Deadline: November 5, 2026 at 5:00 PM ET.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: July 5, 2026 at 5:00 PM ET.", "2026-07-05T17:00:00-04:00"),
    ("Application Deadline: November 5, 2026 at 5:00 pm et.", "2026-11-05T17:00:00-05:00"),
    ("Closing date: November 5, 2026 at 5:00 PM EDT.", "2026-11-05T17:00:00-04:00"),
    ("Closing date: 2026-11-05 at 17:00 UTC.", "2026-11-05T17:00:00+00:00"),
    ("Application Deadline: 2026-11-05T17:00:00.123456Z.", "2026-11-05T17:00:00.123456+00:00"),
    ("Application Deadline: November 5, 2026 at 5:00 PM (America/Toronto).", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: 2026-11-05T17:00:00-05:00.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5:00 PM UTC-05:00.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5 PM ET.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: July 5, 2026 at 5 PM ET.", "2026-07-05T17:00:00-04:00"),
    ("Application Deadline: November 5, 2026 at 5 pm et.", "2026-11-05T17:00:00-05:00"),
    ("Closing date: November 5, 2026 at 5 PM EDT.", "2026-11-05T17:00:00-04:00"),
    ("Closing date: 2026-11-05 at 12 AM UTC.", "2026-11-05T00:00:00+00:00"),
    ("Closing date: 2026-11-05 at 12 PM UTC.", "2026-11-05T12:00:00+00:00"),
    ("Application Deadline: November 5, 2026 at 5 PM (America/Toronto).", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5 PM UTC-05:00.", "2026-11-05T17:00:00-05:00"),
    ("Application Deadline: November 5, 2026 at 5PM -0530.", "2026-11-05T17:00:00-05:30"),
])
def test_existing_extras_positive_formats(text, value):
    got = parse_published_deadline(text)
    assert got == PublishedDeadline(value, text)
    assert datetime.fromisoformat(got.value).tzinfo is not None
    assert explicit_deadline(text) == (value, text)


@pytest.mark.parametrize("label", [
    "Apply by", "Applications close", "Applications close on", "Application closes at",
    "Applications close on:", "Applications close at:", "Applications close on is",
    "Applications deadline:", "Application closing date:", "Applications closing date is",
    "Closing date:", "Closing date of", "Deadline:", "Deadline to apply —",
    "Deadline to apply -", "Date limite :", "Advertised until", "Application Deadline:",
    "formal application deadline is", "formal application deadline",
])
@pytest.mark.parametrize("suffix,value", [
    (".", "2026-11-05"),
    (" at 17:30:15.123456 -0530.", "2026-11-05T17:30:15.123456-05:30"),
])
def test_union_of_labels_keeps_full_clock_precision(label, suffix, value):
    text = f"{label} November 5, 2026{suffix}"
    assert parse_published_deadline(text) == PublishedDeadline(value, text)


@pytest.mark.parametrize("day", [
    "2026-11-05", "November 5, 2026", "November 5 2026", "Nov 5, 2026", "Nov. 5, 2026",
    "November 5th, 2026", "Nov. 5th 2026", "5 November 2026", "5 November, 2026",
    "5 Nov. 2026", "5th November 2026", "NOVEMBER 5TH, 2026",
])
@pytest.mark.parametrize("suffix,value", [
    (".", "2026-11-05"),
    (" by 5:30:15.123 PM GMT+0545.", "2026-11-05T17:30:15.123000+05:45"),
])
def test_union_of_calendar_formats_keeps_full_clock_precision(day, suffix, value):
    text = f"Deadline to apply: {day}{suffix}"
    assert parse_published_deadline(text) == PublishedDeadline(value, text)


@pytest.mark.parametrize("month,number", [
    ("January", "01"), ("February", "02"), ("March", "03"), ("April", "04"),
    ("May", "05"), ("June", "06"), ("July", "07"), ("August", "08"),
    ("September", "09"), ("October", "10"), ("November", "11"), ("December", "12"),
])
def test_all_english_months_have_explicit_year(month, number):
    for rendered in (f"{month} 2nd, 2026", f"{month[:3]}. 2nd, 2026", f"2 {month} 2026"):
        text = f"Apply by {rendered}."
        assert parse_published_deadline(text) == PublishedDeadline(f"2026-{number}-02", text)


@pytest.mark.parametrize("separator", [" at ", " by ", ", ", " à ", " ", "T"])
def test_clock_separators_from_both_parsers(separator):
    text = f"Apply by 2026-11-05{separator}17:30:15 UTC."
    assert parse_published_deadline(text) == PublishedDeadline("2026-11-05T17:30:15+00:00", text)


@pytest.mark.parametrize("clock,value", [
    ("12 AM", "00:00:00"), ("12:00 AM", "00:00:00"), ("12 PM", "12:00:00"),
    ("12:00 PM", "12:00:00"), ("midnight", "00:00:00"), ("noon", "12:00:00"),
    ("12:00:15.25 AM", "00:00:15.250000"), ("12:00:15.25 PM", "12:00:15.250000"),
])
def test_midnight_and_noon_keep_the_published_day(clock, value):
    text = f"Closing date: 2026-11-05 at {clock} UTC."
    assert parse_published_deadline(text) == PublishedDeadline(f"2026-11-05T{value}+00:00", text)


@pytest.mark.parametrize("zone,winter,summer", [
    ("ET", "-05:00", "-04:00"), ("CT", "-06:00", "-05:00"),
    ("MT", "-07:00", "-06:00"), ("PT", "-08:00", "-07:00"),
    ("AT", "-04:00", "-03:00"), ("NT", "-03:30", "-02:30"),
    ("America/Toronto", "-05:00", "-04:00"), ("America/St_Johns", "-03:30", "-02:30"),
    ("America/Argentina/Buenos_Aires", "-03:00", "-03:00"),
    ("Europe/London", "+00:00", "+01:00"),
])
# Historical dates keep this conformance matrix stable across tzdata releases;
# tzdata 2026e changes CT/MT/PT after their spring 2026 transition.
@pytest.mark.parametrize("day,season", [("2025-11-05", "winter"), ("2025-07-05", "summer")])
def test_regional_zones_are_date_sensitive(zone, winter, summer, day, season):
    text = f"Applications close on {day} at 5 PM ({zone})."
    offset = winter if season == "winter" else summer
    assert parse_published_deadline(text) == PublishedDeadline(f"{day}T17:00:00{offset}", text)


@pytest.mark.parametrize("zone,offset", [
    ("UTC", "+00:00"), ("GMT", "+00:00"), ("Z", "+00:00"),
    ("EST", "-05:00"), ("EDT", "-04:00"), ("CST", "-06:00"), ("CDT", "-05:00"),
    ("MST", "-07:00"), ("MDT", "-06:00"), ("PST", "-08:00"), ("PDT", "-07:00"),
    ("AST", "-04:00"), ("ADT", "-03:00"), ("NST", "-03:30"), ("NDT", "-02:30"),
])
@pytest.mark.parametrize("day", ["2026-07-05", "2026-11-05"])
def test_explicit_fixed_abbreviations_are_not_regional(zone, offset, day):
    text = f"Date limite: {day} at 5 PM {zone}."
    assert parse_published_deadline(text) == PublishedDeadline(f"{day}T17:00:00{offset}", text)


@pytest.mark.parametrize("zone", ["ET", "CT", "MT", "PT", "AT", "NT", "America/Toronto", "America/St_Johns"])
@pytest.mark.parametrize("day,clock,uncertainty", [
    ("2025-11-02", "1:30:15.123456 AM", "ambiguous_local_time"),
    ("2025-03-09", "2:30:15.123456 AM", "nonexistent_local_time"),
    ("2025-11-02", "1 AM", "ambiguous_local_time"),
    ("2025-03-09", "2 AM", "nonexistent_local_time"),
])
def test_dst_fold_and_gap_never_choose_an_offset(zone, day, clock, uncertainty):
    text = f"Apply by {day} at {clock} {zone}."
    assert parse_published_deadline(text) == PublishedDeadline(day, text, uncertainty)
    assert explicit_deadline(text) == (day, text)


@pytest.mark.parametrize("day,clock,zone,value", [
    ("2026-11-01", "1:30 AM", "EST", "2026-11-01T01:30:00-05:00"),
    ("2026-11-01", "1:30 AM", "EDT", "2026-11-01T01:30:00-04:00"),
    ("2026-03-08", "2:30 AM", "-05:00", "2026-03-08T02:30:00-05:00"),
    ("2026-11-01", "12:30 AM", "ET", "2026-11-01T00:30:00-04:00"),
    ("2026-11-01", "2:30 AM", "ET", "2026-11-01T02:30:00-05:00"),
    ("2026-03-08", "1:30 AM", "ET", "2026-03-08T01:30:00-05:00"),
    ("2026-03-08", "3:30 AM", "ET", "2026-03-08T03:30:00-04:00"),
])
def test_valid_times_around_dst_and_explicit_offsets_remain_exact(day, clock, zone, value):
    text = f"Application Deadline: {day} at {clock} {zone}."
    assert parse_published_deadline(text) == PublishedDeadline(value, text)


@pytest.mark.parametrize("zone,offset", [
    ("-0530", "-05:30"), ("+05:45", "+05:45"), ("UTC+0545", "+05:45"),
    ("GMT-0330", "-03:30"), ("utc-05:00", "-05:00"), ("+0000", "+00:00"),
    ("+00:00", "+00:00"), ("GMT +05:45", "+05:45"),
])
def test_numeric_offsets_preserve_seconds_and_fraction(zone, offset):
    text = f"Apply by November 5th, 2026 at 17:30:15.123456 {zone}."
    got = parse_published_deadline(text)
    assert got == PublishedDeadline(f"2026-11-05T17:30:15.123456{offset}", text)
    assert datetime.fromisoformat(got.value).microsecond == 123456


@pytest.mark.parametrize("fraction,microseconds", [
    ("1", "100000"), ("12", "120000"), ("123", "123000"),
    ("1234", "123400"), ("12345", "123450"), ("123456", "123456"),
])
def test_fractional_seconds_are_not_truncated(fraction, microseconds):
    text = f"Deadline: 2026-11-05T17:30:15.{fraction}Z."
    assert parse_published_deadline(text) == PublishedDeadline(f"2026-11-05T17:30:15.{microseconds}+00:00", text)


@pytest.mark.parametrize("clock,uncertainty", [
    ("5:00 PM", "missing_timezone"), ("5 PM", "missing_timezone"),
    ("17:30:15.123456", "missing_timezone"),
    ("5:00 PM XYZ", "unknown_timezone"), ("5 PM XYZ", "unknown_timezone"),
    ("5 PM xyz", "unknown_timezone"), ("5 PM (Mars/Olympus)", "unknown_timezone"),
    ("5 PM -00:00", "unknown_timezone"), ("5 PM -0000", "unknown_timezone"),
    ("5 PM UTC-00:00", "unknown_timezone"),
    ("5 PM +24:00", "unknown_timezone"), ("5 PM +05:60", "unknown_timezone"),
    ("5 PM UTC+garbage", "unknown_timezone"), ("5 PM ETfoo", "unknown_timezone"),
    ("5 PM -05:000", "unknown_timezone"), ("5 PM -05:00:30", "unknown_timezone"),
    ("5 PM +05:30.5", "unknown_timezone"), ("5 PM (ET", "unknown_timezone"),
    ("13 PM ET", "invalid_clock"), ("0 AM ET", "invalid_clock"),
    ("25:00 ET", "invalid_clock"), ("24:00 UTC", "invalid_clock"),
    ("5:60 PM ET", "invalid_clock"), ("17:30:60 UTC", "invalid_clock"),
    ("17:30:150 UTC", "invalid_clock"), ("17:3 UTC", "invalid_clock"),
    ("17:30:15:05 UTC", "invalid_clock"), ("17:30:15.1234567 UTC", "invalid_clock"),
    ("17:30:15.1.2 UTC", "invalid_clock"), ("17:30.5 UTC", "invalid_clock"),
    ("5 ET", "invalid_clock"), ("-1:30 ET", "invalid_clock"),
    ("13 PM", "invalid_clock"),
])
def test_uncertain_and_invalid_clocks_keep_date_and_unmodified_evidence(clock, uncertainty):
    text = f"Application Deadline: November 5, 2026 at {clock}."
    assert parse_published_deadline(text) == PublishedDeadline("2026-11-05", text, uncertainty)
    assert explicit_deadline(text) == ("2026-11-05", text)


@pytest.mark.parametrize("text", [
    "", "November 5, 2026", "2026-11-05", "Posted November 5, 2026. Apply soon.",
    "Applications reviewed on a rolling basis. Start January 2027.",
    "Apply by tomorrow.", "Apply by November 5.", "Apply by Nov. 5th.", "Apply by 5 November.",
    "Apply by November 5, 26.", "Advertised until yesterday.", "Closing date: 11/05.",
    "Application Deadline: 2026-02-30.", "Application Deadline: February 29, 2026.",
    "Application Deadline: November 31, 2026.", "Deadline: 2026-13-05.",
    "Deadline: November 5, 20267.", "Deadline: 2026-11-051.",
    "Application Deadline: 0000-01-01.", "Next deadline is six weeks after posting.",
])
def test_no_relative_yearless_posting_or_invalid_calendar_inference(text):
    assert parse_published_deadline(text) is None
    assert explicit_deadline(text) == (None, None)


@pytest.mark.parametrize("text,value", [
    ("Advertised until February 29, 2028.", "2028-02-29"),
    ("Apply by January 2, 1999.", "1999-01-02"),
    ("Closing date: 2099-01-02.", "2099-01-02"),
])
def test_full_explicit_calendar_year_is_not_inferred_or_clamped(text, value):
    assert parse_published_deadline(text) == PublishedDeadline(value, text)


RBC_NOTE = "Note: Applications will be accepted until 11:59 PM on the day prior to the application deadline date above."


@pytest.mark.parametrize("displayed,value", [
    ("2026-11-15", "2026-11-14"), ("2026-11-15 at 5 PM ET.", "2026-11-14"),
    ("2027-01-01", "2026-12-31"), ("2028-03-01", "2028-02-29"),
])
@pytest.mark.parametrize("gap", [" ", "\n\n", "  Additional application details.\n"])
def test_rbc_prior_day_note_keeps_original_span_without_assuming_its_timezone(displayed, value, gap):
    text = f"Application Deadline: {displayed}{gap}{RBC_NOTE}"
    got = parse_published_deadline(text)
    assert got == PublishedDeadline(value, text, "missing_timezone")
    assert explicit_deadline(text) == (value, text)


@pytest.mark.parametrize("reverse", [False, True])
def test_rbc_formal_prose_deadline_wins_over_displayed_metadata(reverse):
    displayed = f"Application Deadline: 2026-11-15. {RBC_NOTE}"
    formal = "formal application deadline is November 13, 2026 at 5:30:15 PM ET."
    text = f"{formal}\n{displayed}" if reverse else f"{displayed}\n{formal}"
    assert parse_published_deadline(text) == PublishedDeadline("2026-11-13T17:30:15-05:00", formal)


@pytest.mark.parametrize("label", ["Closing date:", "Advertised until", "Apply by"])
def test_rbc_note_is_not_a_generic_subtract_one_day_rule(label):
    clause = f"{label} 2026-11-15."
    assert parse_published_deadline(f"{clause} {RBC_NOTE}") == PublishedDeadline("2026-11-15", clause)


def test_evidence_preserves_original_whitespace_case_and_entire_clock_clause():
    clause = "aPpLiCaTiOn  DeAdLiNe:\nNov. 5th, 2026 at  5:30:15.123 PM (America/Toronto), online only."
    text = f"Posted October 24, 2026. {clause} Please attach a resume."
    assert parse_published_deadline(text) == PublishedDeadline("2026-11-05T17:30:15.123000-05:00", clause)


def test_a_later_valid_label_is_not_lost_after_an_invalid_calendar():
    clause = "Apply by November 5, 2026."
    assert parse_published_deadline(f"Closing date: 2026-02-30. {clause}") == PublishedDeadline("2026-11-05", clause)


def test_published_deadline_is_frozen_and_uncertainty_defaults_to_none():
    got = PublishedDeadline("2026-11-05", "Apply by November 5, 2026.")
    assert got.uncertainty is None
    with pytest.raises(FrozenInstanceError):
        got.value = "2026-11-06"


def test_precision_survives_normal_iso_utc_conversion():
    got = parse_published_deadline("Deadline: 2026-11-05T17:30:15.123456-0530.")
    assert datetime.fromisoformat(got.value).astimezone(timezone.utc).isoformat() == "2026-11-05T23:00:15.123456+00:00"


@pytest.mark.parametrize("clock,value", [
    ("5\nPM\nET", "2026-11-05T17:00:00-05:00"),
    ("5:30\nPM\nET", "2026-11-05T17:30:00-05:00"),
    ("17:30:15\nUTC", "2026-11-05T17:30:15+00:00"),
    ("17:30:15.000000Z", "2026-11-05T17:30:15.000000+00:00"),
])
def test_clock_whitespace_and_explicit_zero_fraction_are_not_lost(clock, value):
    text = f"Application Deadline: November 5, 2026 at {clock}."
    assert parse_published_deadline(text) == PublishedDeadline(value, text)


@pytest.mark.parametrize("text,uncertainty", [
    ("Application Deadline: November 1, 2026 at 1 AM ET.", "ambiguous_local_time"),
    ("Application Deadline: March 8, 2026 at 2 AM ET.", "nonexistent_local_time"),
])
def test_existing_extras_hour_only_dst_controls(text, uncertainty):
    got = parse_published_deadline(text)
    assert got is not None and "T" not in got.value
    assert got.uncertainty == uncertainty and got.evidence == text


def test_rbc_note_does_not_attach_to_a_different_intervening_deadline():
    clause = "Application Deadline: 2026-11-15."
    text = f"{clause} Apply by November 18, 2026. {RBC_NOTE}"
    assert parse_published_deadline(text) == PublishedDeadline("2026-11-15", clause)


def test_rbc_prior_day_overflow_does_not_invent_a_calendar_day():
    assert parse_published_deadline(f"Application Deadline: 0001-01-01. {RBC_NOTE}") is None


def test_parser_has_only_pure_standard_library_dependencies():
    tree = ast.parse(inspect.getsource(deadlines))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "datetime", "re", "zoneinfo"}
