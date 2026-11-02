"""Pure URL-policy fixtures: no source requests, application imports, or DBs."""
from __future__ import annotations

import ipaddress
import json
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from career_radar.url_safety import UnsafeURL, canonical_job_url, is_public_ip, normalize_public_url

FIXTURES = json.loads((Path(__file__).parent / "fixtures/url_safety_cases.json").read_text())
CASES = FIXTURES["cases"]


@pytest.fixture(autouse=True)
def no_network_or_databases(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("URL helpers must not perform DNS, network, or DB work")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, denied)
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(sqlite3, "connect", denied)


def test_shared_fixture_ids_are_unique():
    for group in FIXTURES.values():
        assert group and len({case["id"] for case in group}) == len(group)
    assert all(set(case) == {"id", "value", "normalized", "canonical"} for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_shared_fetch_url_policy(case):
    if case["normalized"] is None:
        with pytest.raises(UnsafeURL):
            normalize_public_url(case["value"])
    else:
        actual = normalize_public_url(case["value"])
        assert actual == case["normalized"]
        assert normalize_public_url(actual) == actual
        # The emitted host, rather than merely a checked temporary value, is
        # exactly the ASCII IDNA authority HTTPX would use on the original URL.
        assert urlsplit(actual).hostname == httpx.URL(case["value"]).raw_host.decode("ascii").lower()
        assert urlsplit(actual).hostname.isascii()
        original = urlsplit(case["value"])
        emitted = urlsplit(actual)
        assert (emitted.path, emitted.query, emitted.fragment) == (original.path, original.query, original.fragment)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_shared_job_link_policy_and_canonical_identity(case):
    actual = canonical_job_url(case["value"])
    assert actual == case["canonical"]
    if actual is not None:
        assert canonical_job_url(actual) == actual
        assert normalize_public_url(case["value"], standard_ports_only=True, allow_public_ip=False) == case["normalized"]
    else:
        with pytest.raises(UnsafeURL):
            normalize_public_url(case["value"], standard_ports_only=True, allow_public_ip=False)


@pytest.mark.parametrize("case", FIXTURES["equivalent"], ids=lambda case: case["id"])
def test_authority_equivalent_urls_produce_one_negative_evidence_lookup_key(case):
    """Helper-level identity proof, NOT a claim about pending core integration."""
    keys = [canonical_job_url(value) for value in case["urls"]]
    assert None not in keys
    assert len(set(keys)) == 1
    for direct in keys:
        negative_evidence = {direct: "synthetic authoritative rejection"}
        assert all(key in negative_evidence for key in keys)


@pytest.mark.parametrize("case", FIXTURES["distinct"], ids=lambda case: case["id"])
def test_distinct_requisitions_and_unproven_aliases_keep_distinct_keys(case):
    keys = [canonical_job_url(value) for value in case["urls"]]
    assert None not in keys
    assert len(set(keys)) == len(keys)
    # An applied-state/negative-evidence index cannot match the other identity.
    assert all(other not in {keys[0]: "synthetic applied"} for other in keys[1:])


@pytest.mark.parametrize("value,standard,allow_ip,expected", [
    ("https://employer.example:8443/jobs", False, False, "https://employer.example:8443/jobs"),
    ("https://employer.example:8443/jobs", True, False, None),
    ("https://8.8.8.8/jobs", True, True, "https://8.8.8.8/jobs"),
    ("https://8.8.8.8/jobs", False, False, None),
    ("https://8.8.8.8:8443/jobs", False, True, "https://8.8.8.8:8443/jobs"),
    ("https://8.8.8.8:8443/jobs", True, True, None),
    ("https://[2606:4700:4700::1111]:443/jobs", True, True, "https://[2606:4700:4700::1111]/jobs"),
    ("https://[2606:4700:4700::1111]/jobs", False, False, None),
    ("http://10.1.2.3:8080/jobs", False, True, None),
])
def test_fetcher_flags_are_independent(value, standard, allow_ip, expected):
    if expected is None:
        with pytest.raises(UnsafeURL):
            normalize_public_url(value, standard_ports_only=standard, allow_public_ip=allow_ip)
    else:
        assert normalize_public_url(value, standard_ports_only=standard, allow_public_ip=allow_ip) == expected


@pytest.mark.parametrize("value,expected", [
    ("8.8.8.8", True), ("1.1.1.1", True),
    ("2606:4700:4700::1111", True), ("2001:4860:4860::8888", True),
    ("0.0.0.0", False), ("127.0.0.1", False), ("10.1.2.3", False),
    ("172.16.1.1", False), ("192.168.1.1", False), ("169.254.169.254", False),
    ("100.64.0.1", False), ("192.0.2.1", False), ("198.51.100.1", False),
    ("203.0.113.1", False), ("224.0.0.1", False), ("240.0.0.1", False),
    ("255.255.255.255", False), ("::", False), ("::1", False),
    ("2001:db8::1", False), ("fe80::1", False), ("fec0::1", False),
    ("fd00::1", False), ("ff02::1", False), ("2606:4700:4700::1111%eth0", False),
    ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False),
    ("64:ff9b::7f00:1", False), ("64:ff9b::a00:1", False),
    ("64:ff9b:1::a00:1", False), ("2002:7f00:1::", False),
    ("2001:0:808:808:0:ffff:80ff:fffe", False),
    ("2001:0:7f00:1:0:ffff:f7f7:f7f7", False),
])
def test_public_ip_policy_for_strings_and_ipaddress_objects(value, expected):
    assert is_public_ip(value) is expected
    assert is_public_ip(ipaddress.ip_address(value)) is expected


@pytest.mark.parametrize("value", [
    None, True, 134744072, b"8.8.8.8", "", "8.8", "0x08080808", "010.010.010.010",
    " 8.8.8.8", "8.8.8.8 ", "８.８.８.８", "public.example", "[2606:4700:4700::1111]",
])
def test_invalid_address_tokens_are_not_coerced_into_valid_literals(value):
    assert is_public_ip(value) is False


@pytest.mark.parametrize("value", [
    "::ffff:127.0.0.1", "64:ff9b::7f00:1", "64:ff9b::a00:1",
    "64:ff9b:1::a00:1", "2002:7f00:1::",
    "2001:0:808:808:0:ffff:80ff:fffe", "2001:0:7f00:1:0:ffff:f7f7:f7f7",
])
def test_embedded_private_destinations_do_not_depend_on_runtime_global_tables(value):
    # ipaddress's global/reserved tables differ across supported Python releases.
    # Exercise embedded-address checks even when a release calls the envelope global.
    class ApparentlyGlobalIPv6(ipaddress.IPv6Address):
        @property
        def is_global(self):
            return True

        @property
        def is_reserved(self):
            return False

    assert is_public_ip(ApparentlyGlobalIPv6(value)) is False


@pytest.mark.parametrize("label", ["a" * 64, "-jobs", "jobs-", "jobs_feed", "", "a\ud800"])
def test_malformed_dns_labels_are_not_repaired(label):
    with pytest.raises(UnsafeURL):
        normalize_public_url(f"https://{label}.example/jobs")
    assert canonical_job_url(f"https://{label}.example/jobs") is None


def test_dns_length_limits_apply_after_idna_encoding():
    valid = ".".join(["a" * 63] * 3 + ["b" * 61])
    assert len(valid) == 253
    assert normalize_public_url(f"https://{valid}/jobs") == f"https://{valid}/jobs"
    for host in (valid + "x", ".".join(["é" * 58, "example"])):
        with pytest.raises(UnsafeURL):
            normalize_public_url(f"https://{host}/jobs")


def test_tracking_allowlist_preserves_unknown_names_and_exact_nontracking_bytes():
    query = "ref=%FF&refid=REQ-A&source=opaque+value&position=REQ-B&trackingid=REQ-C"
    for key in ("utm_source", "UTM_campaign", "gclid", "dclid", "fbclid", "msclkid", "gbraid", "wbraid", "twclid", "ttclid", "yclid"):
        assert canonical_job_url(f"https://employer.example/jobs?{key}=click&{query}#route") == f"https://employer.example/jobs?{query}#route"
    for key in ("ref", "refid", "source", "position", "referrer", "trackingid", "trk", "lever-source", "lever-origin", "pagenum", "originalsubdomain", "custom_click_id", "%FF"):
        value = f"https://employer.example/jobs?{key}=opaque+%FF&{query}#route"
        assert canonical_job_url(value) == value


def test_error_contract_is_value_error_and_does_not_echo_credentials():
    with pytest.raises(UnsafeURL) as raised:
        normalize_public_url("https://synthetic-user:synthetic-password@employer.example/jobs")
    assert isinstance(raised.value, ValueError)
    assert "synthetic-password" not in str(raised.value)
