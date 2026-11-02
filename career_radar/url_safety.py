"""Pure public-URL policy and conservative job-link identity.

This is a lexical boundary, not a DNS/rebinding defense: fetchers must still
validate every resolved address and pin connections, including redirects. Job
links require public DNS names on ports 80/443; fetchers may explicitly retain
global IP literals and other ports. No application/database imports or network I/O.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit

import httpx
import idna  # HTTPX's existing IDNA2008 dependency, not Python's IDNA2003 codec.

__all__ = ["UnsafeURL", "normalize_public_url", "is_public_ip", "canonical_job_url"]

_URL = re.compile(r"\A(https?)://([^/?#]+)(.*)\Z", re.I)
_AUTHORITY = re.compile(r"(?:\[[^\[\]]+\]|[^:\[\]]+)(?::[0-9]+)?\Z")
_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x20\x7f\\\ud800-\udfff]")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)\Z", re.I)
_LOCAL_NAMES = ("localhost", "localdomain", "local", "lan", "internal", "home", "home.arpa")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_CLICK_IDS = frozenset({"gclid", "dclid", "fbclid", "msclkid", "gbraid", "wbraid", "twclid", "ttclid", "yclid"})


class UnsafeURL(ValueError):
    """A value is not an unambiguous public HTTP(S) URL under this policy."""


def is_public_ip(address: object) -> bool:
    """Check an IP object or an exact literal string, never a numeric alias.

    Keep the extras fetcher's conservative global/multicast/reserved checks AND
    inspect transition envelopes: a global-looking IPv6 address must not hide a
    private IPv4 endpoint. Python releases differ in their special-range tables.
    """
    if isinstance(address, str):
        try:
            address = ipaddress.ip_address(address)
        except ValueError:
            return False
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id or address.is_site_local or address in _NAT64_LOCAL:
            return False
        embedded = [address.ipv4_mapped, address.sixtofour, *(address.teredo or ())]
        if address in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(address) & 0xffffffff))
        if any(ip is not None and not is_public_ip(ip) for ip in embedded):
            return False
    return True


def normalize_public_url(
    value: object, *, standard_ports_only: bool = False, allow_public_ip: bool = True
) -> str:
    """Validate and emit an ASCII-host HTTP(S) URL, or raise ``UnsafeURL``.

    Use HTTPX's actual IDNA2008 transport spelling (not builtin IDNA2003, which
    can silently change domains such as faß.example to fass.example). Only the
    scheme/host case and numeric/default port are normalized. Preserve the exact
    path/query/fragment suffix, even empty delimiters, duplicate fields and an
    empty path. Do not trim whitespace, HTML-unescape, or repair malformed input.
    """
    if not isinstance(value, str):
        raise UnsafeURL("Missing or non-string public URL")
    if _UNSAFE_CHARACTERS.search(value):
        raise UnsafeURL("Unsafe characters in public URL")
    matched = _URL.fullmatch(value)
    if matched is None:
        raise UnsafeURL("Public URL requires an absolute HTTP(S) authority")
    scheme, authority, suffix = matched.groups()
    if "@" in authority or "%" in authority or _AUTHORITY.fullmatch(authority) is None:
        raise UnsafeURL("Credentials, encoded hosts, or malformed authority in public URL")
    try:
        parts = urlsplit(value)
        port = parts.port  # Validates ASCII decimal syntax and the 0..65535 range.
        if not parts.hostname or parts.username is not None or parts.password is not None or port == 0:
            raise ValueError("Invalid host, credentials, or port")
        if standard_ports_only and port not in (None, 80, 443):
            raise ValueError("Job links require ports 80 or 443")
        parsed = httpx.URL(value)
        host = parsed.raw_host.decode("ascii").lower()
    except (ValueError, UnicodeError, httpx.InvalidURL) as exc:
        # Do not echo a rejected URL; it may contain credentials or private data.
        raise UnsafeURL("Unsafe or invalid public URL authority") from exc
    if not host or "%" in host:
        raise UnsafeURL("Encoded or scoped host is not a public URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        name = host.removesuffix(".")
        if "." not in name or any(name == local or name.endswith("." + local) for local in _LOCAL_NAMES):
            raise UnsafeURL("Local host is not a public URL")
        labels = name.split(".")
        if len(name) > 253 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise UnsafeURL("Invalid public URL hostname")
        # WHATWG treats a numeric final label as an IPv4 candidate, even if an
        # earlier label is nonnumeric. Do not let browser/libc aliases reach DNS.
        if _NUMERIC_LABEL.fullmatch(labels[-1]):
            raise UnsafeURL("Ambiguous numeric IP URL is not permitted")
        # HTTPX passes ASCII hostnames through without validating A-labels.
        # Validate every punycode label with its own IDNA library; otherwise an
        # invalid xn-- label can be emitted even though a browser rejects it.
        try:
            for label in labels:
                if label.startswith("xn--"):
                    idna.decode(label)
        except idna.IDNAError as exc:
            raise UnsafeURL("Invalid IDNA public URL hostname") from exc
        emitted_host = host
    else:
        if not allow_public_ip or not is_public_ip(address):
            raise UnsafeURL("Literal IP is not permitted by this public URL policy")
        emitted_host = f"[{host}]" if address.version == 6 else host
    # Do NOT emit str(parsed): HTTPX can quote or collapse path segments,
    # which is lossy for stored job identity. Normalize the scheme before the
    # default-port comparison; HTTPX versions differ for uppercase input schemes.
    scheme = scheme.lower()
    default_port = 443 if scheme == "https" else 80
    emitted_port = f":{port}" if port not in (None, default_port) else ""
    return f"{scheme}://{emitted_host}{emitted_port}{suffix}"


def _is_tracking_field(field: str) -> bool:
    # Decode only a parameter NAME to recognize an established tracking key.
    # Never round-trip opaque values through parse_qsl/urlencode or sort fields.
    try:
        key = unquote(field.partition("=")[0], encoding="ascii", errors="strict")
    except UnicodeError:
        return False
    if not key.isascii():
        return False
    key = key.lower()
    return key.startswith("utm_") or key in _CLICK_IDS


def canonical_job_url(value: object) -> str | None:
    """Return a safe, non-lossy job identity URL; return ``None`` when invalid.

    Remove only UTM parameters and an explicit established click-ID allowlist.
    ``position``, ``ref``, ``refid``, ``source``, other unknown keys, duplicate
    order, opaque encodings, trailing slashes and meaningful fragments survive.
    """
    try:
        normalized = normalize_public_url(value, standard_ports_only=True, allow_public_ip=False)
    except UnsafeURL:
        return None
    before_fragment, fragment_marker, fragment = normalized.partition("#")
    base, query_marker, query = before_fragment.partition("?")
    if not query_marker:
        return normalized
    fields = [field for field in query.split("&") if not _is_tracking_field(field)]
    kept_query = "?" + "&".join(fields) if fields else ""
    return base + kept_query + fragment_marker + fragment
