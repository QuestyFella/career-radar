"""Job-search regions: where a posting must be located to reach your radar.

A region is a country (ISO 3166-1 alpha-2 code), ``global`` for no geographic
restriction, or ``custom`` with a label and free-text location terms. Country
aliases include the English name, official/common names, ISO codes, first-level
subdivisions and a short list of large hiring cities. Matching is deliberately
conservative: a posting is kept only when its location names the region, a place
inside it, or an unrestricted remote role. A remote role that names another
country is not treated as eligible.
"""
from __future__ import annotations

import functools
import re
import unicodedata
from collections import OrderedDict

import pycountry

DEFAULT_REGION = {"id": "ca", "label": "Canada", "terms": [], "include_remote": True}

# Common two-letter country codes that are also US state postal codes. Matching
# them alone would turn "Indianapolis, IN" into India or "Austin, CA" into Canada.
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}

# A few large hiring cities per common region. Country and subdivision names do
# most of the work; cities handle postings that name only "Berlin" or "Austin".
_CITIES = {
    "ca": ("Toronto", "Ottawa", "Montréal", "Montreal", "Vancouver", "Waterloo", "Kitchener",
           "Mississauga", "Guelph", "Calgary", "Edmonton", "Winnipeg", "Halifax", "Victoria",
           "Burnaby", "Markham", "Brampton", "Oakville", "Québec", "Quebec", "Sherbrooke", "Laval",
           "Saskatoon", "Regina", "Fredericton", "Moncton", "Kanata", "Burlington", "Richmond Hill",
           "Cambridge", "St. John's", "London, Ontario"),
    "us": ("New York", "San Francisco", "Los Angeles", "Seattle", "Austin", "Boston", "Chicago",
           "Denver", "Atlanta", "Dallas", "Houston", "Miami", "Phoenix", "Philadelphia", "Pittsburgh",
           "San Diego", "San Jose", "Portland", "Salt Lake City", "Minneapolis", "Detroit", "Raleigh",
           "Washington, DC", "Bay Area", "Silicon Valley", "Brooklyn"),
    "gb": ("London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Edinburgh", "Bristol", "Oxford",
           "Cambridge", "Cardiff", "Belfast", "Sheffield", "Liverpool", "Nottingham", "Reading"),
    "au": ("Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Canberra", "Hobart", "Darwin",
           "Gold Coast", "Newcastle"),
    "nz": ("Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga", "Dunedin"),
    "de": ("Berlin", "Munich", "München", "Hamburg", "Frankfurt", "Cologne", "Köln", "Stuttgart",
           "Düsseldorf", "Dresden", "Leipzig", "Nuremberg"),
    "fr": ("Paris", "Lyon", "Toulouse", "Marseille", "Bordeaux", "Nantes", "Grenoble", "Lille",
           "Sophia Antipolis"),
    "nl": ("Amsterdam", "Rotterdam", "The Hague", "Den Haag", "Utrecht", "Eindhoven", "Delft"),
    "ie": ("Dublin", "Cork", "Galway", "Limerick", "Waterford"),
    "in": ("Bengaluru", "Bangalore", "Mumbai", "Delhi", "New Delhi", "Hyderabad", "Pune", "Chennai",
           "Noida", "Gurgaon", "Gurugram", "Kolkata", "Ahmedabad"),
    "sg": ("Singapore",),
    "jp": ("Tokyo", "Osaka", "Kyoto", "Yokohama", "Nagoya", "Fukuoka"),
    "es": ("Madrid", "Barcelona", "Valencia", "Seville", "Málaga", "Bilbao"),
    "it": ("Milan", "Milano", "Rome", "Roma", "Turin", "Torino", "Bologna"),
    "se": ("Stockholm", "Gothenburg", "Göteborg", "Malmö", "Uppsala"),
    "ch": ("Zurich", "Zürich", "Geneva", "Basel", "Lausanne", "Bern"),
    "br": ("São Paulo", "Sao Paulo", "Rio de Janeiro", "Belo Horizonte", "Curitiba", "Porto Alegre"),
    "mx": ("Mexico City", "Ciudad de México", "Guadalajara", "Monterrey", "Querétaro"),
    "za": ("Cape Town", "Johannesburg", "Pretoria", "Durban", "Stellenbosch"),
    "ae": ("Dubai", "Abu Dhabi", "Sharjah"),
    "il": ("Tel Aviv", "Haifa", "Jerusalem", "Herzliya"),
    "pl": ("Warsaw", "Kraków", "Krakow", "Wrocław", "Wroclaw", "Gdańsk", "Gdansk", "Poznań"),
    "pt": ("Lisbon", "Lisboa", "Porto", "Braga"),
    "kr": ("Seoul", "Busan", "Incheon", "Daejeon"),
    "cn": ("Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Guangzhou", "Chengdu"),
}

_GLOBAL_IDS = {"global", "any", "anywhere", "worldwide", "all"}
_CUSTOM_IDS = {"custom", "other", "elsewhere"}


def fold(text) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def default_region() -> dict:
    return dict(DEFAULT_REGION, terms=[])


def _terms(value) -> list[str]:
    rows = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    result = []
    for item in rows:
        text = re.sub(r"\s+", " ", str(item or "")).strip(" ,;")
        if text and text.casefold() not in {row.casefold() for row in result}:
            result.append(text[:80])
    return result[:40]


def _country(ident: str):
    ident = str(ident or "").strip()
    if not ident:
        return None
    getters = (("alpha_2", ident.upper()), ("alpha_3", ident.upper()), ("name", ident))
    for field, value in getters:
        try:
            country = pycountry.countries.get(**{field: value})
        except (KeyError, LookupError):
            country = None
        if country:
            return country
    return None


def normalize_region(value) -> dict:
    """Validate a stored/requested region; invalid input falls back to Canada."""
    if isinstance(value, str):
        value = {"id": value}
    if not isinstance(value, dict):
        return default_region()
    ident = str(value.get("id") or "").strip()
    label = re.sub(r"\s+", " ", str(value.get("label") or "")).strip()[:80]
    terms = _terms(value.get("terms"))
    include_remote = value.get("include_remote", True) is not False
    if ident.casefold() in _GLOBAL_IDS:
        return {"id": "global", "label": label or "Anywhere", "terms": terms, "include_remote": include_remote}
    if ident.casefold() in _CUSTOM_IDS:
        custom_label = label or "Custom region"
        return {"id": "custom", "label": custom_label, "terms": _terms([custom_label, *terms]),
                "include_remote": include_remote}
    country = _country(ident)
    if country:
        return {"id": country.alpha_2.casefold(), "label": label or country.name, "terms": terms,
                "include_remote": include_remote}
    # A free-form region name is still useful: treat everything the user typed as terms.
    if label:
        return {"id": "custom", "label": label, "terms": _terms([label, *terms]), "include_remote": include_remote}
    return default_region()


def is_global(region) -> bool:
    return normalize_region(region)["id"] == "global"


def is_canada(region) -> bool:
    return normalize_region(region)["id"] == "ca"


def is_custom(region) -> bool:
    return normalize_region(region)["id"] == "custom"


def label(region) -> str:
    return normalize_region(region)["label"]


def country_code(region) -> str | None:
    region = normalize_region(region)
    return region["id"].upper() if len(region["id"]) == 2 else None


def country_name(region) -> str | None:
    code = country_code(region)
    country = pycountry.countries.get(alpha_2=code) if code else None
    return country.name if country else None


def _subdivision_aliases(code: str) -> tuple[list[str], list[str]]:
    """First-level subdivision names and codes (safe to match) for a country."""
    try:
        rows = pycountry.subdivisions.get(country_code=code) or []
    except (KeyError, LookupError):
        return [], []
    names, codes = [], []
    for row in rows:
        name = re.sub(r"\s*\[[^\]]*\]\s*", " ", str(row.name or "")).strip()
        if len(name) >= 3:
            names.append(name)
        short = str(row.code or "").split("-", 1)[-1]
        if len(short) >= 2:
            codes.append(short)
    return names, codes


@functools.lru_cache(maxsize=512)
def _alias_pattern(region_id: str, terms: tuple[str, ...] = ()) -> re.Pattern | None:
    region = normalize_region({"id": region_id, "terms": list(terms)})
    values = list(region["terms"])
    codes: list[str] = []
    bare_codes: list[str] = []
    if region["id"] == "global":
        return None
    if region["id"] == "custom":
        values.extend(_CITIES.get(region["id"], ()))
    else:
        code = region["id"].upper()
        country = pycountry.countries.get(alpha_2=code)
        if country:
            values.extend(v for v in (country.name, getattr(country, "official_name", None),
                                      getattr(country, "common_name", None)) if v)
            values.append(country.alpha_3)
            if country.alpha_2 not in _US_STATE_CODES:
                bare_codes.append(country.alpha_2)
            codes.append(country.alpha_2)
            if code == "GB":
                values.extend(("UK", "U.K.", "United Kingdom", "Great Britain", "England", "Scotland",
                               "Wales", "Northern Ireland"))
                bare_codes.append("UK")
            if code == "US":
                values.extend(("USA", "U.S.", "U.S.A.", "United States of America", "America"))
                bare_codes.append("US")
            if code == "AE":
                values.append("UAE")
        values.extend(_CITIES.get(region["id"], ()))
        names, sub_codes = _subdivision_aliases(code)
        values.extend(names)
        codes.extend(sub_codes)
    patterns = []
    for value in sorted({str(v).strip() for v in values if str(v).strip()}, key=len, reverse=True):
        folded = fold(value)
        if not folded:
            continue
        body = re.escape(folded).replace(r"\ ", r"[\s,./&-]+").replace(r"\-", r"[\s-]?")
        patterns.append(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")
    for value in sorted({str(v).strip().upper() for v in codes if v}, key=len, reverse=True):
        if len(value) < 2:
            continue
        # Subdivision/country codes are only trusted after a comma ("Austin, TX")
        # so ordinary words never match.
        patterns.append(r",\s*" + re.escape(value.casefold()) + r"(?![a-z0-9])")
    for value in sorted({str(v).strip().upper() for v in bare_codes if v}, key=len, reverse=True):
        # Common country abbreviations ("Remote - US", "UK") are matched only in
        # capitals, so the English word "us" is not a location.
        patterns.append(r"(?-i:(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9]))")
    return re.compile("|".join(patterns), re.I) if patterns else None


@functools.lru_cache(maxsize=1)
def _all_country_pattern() -> re.Pattern | None:
    values, codes = [], []
    for country in pycountry.countries:
        values.extend(v for v in (country.name, getattr(country, "official_name", None),
                                  getattr(country, "common_name", None)) if v)
        codes.append(country.alpha_3)
    patterns = []
    for value in sorted({str(v).strip() for v in values if str(v).strip()}, key=len, reverse=True):
        folded = fold(value)
        if not folded:
            continue
        body = re.escape(folded).replace(r"\ ", r"[\s,./&-]+").replace(r"\-", r"[\s-]?")
        patterns.append(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")
    for value in sorted({str(v).strip().upper() for v in codes if v}, key=len, reverse=True):
        patterns.append(r"(?<![a-z0-9])" + re.escape(value.casefold()) + r"(?![a-z0-9])")
    patterns.extend((r"\busa\b", r"\bu\.s\.?a?\b", r"\buk\b", r"\bu\.k\.\b",
                     r"(?-i:(?<![A-Za-z0-9])(?:US|USA|UK|UAE)(?![A-Za-z0-9]))"))
    return re.compile("|".join(patterns), re.I) if patterns else None


def _mentions_other_country(text: str, region: dict) -> bool:
    """True when a location names a country other than the selected one."""
    folded = fold(text)
    own = country_code(region)
    if own:
        country = pycountry.countries.get(alpha_2=own)
        names = {fold(country.name)} if country else set()
        names |= {fold(getattr(country, field, "")) for field in ("official_name", "common_name")}
        own_pattern = _alias_pattern(region["id"], tuple(region["terms"]))
        if own_pattern and (own_pattern.search(folded) or own_pattern.search(text)):
            # Remove this region's own names before looking for another country.
            for name in sorted((n for n in names if n), key=len, reverse=True):
                folded = folded.replace(name, " ")
            if own_pattern.search(folded) or own_pattern.search(text):
                # Codes/subdivision names still present; let the own-region match win.
                return False
    other = _all_country_pattern()
    return bool(other and (other.search(folded) or other.search(text)))


def _segments(location: str) -> list[str]:
    return [part.strip(" ()[]") for part in re.split(r"\s*(?:\||;|\n| / |\bor\b)\s*", str(location or ""))
            if part.strip(" ()[]")]


def _region_names(region: dict) -> list[str]:
    names = list(region["terms"])
    code = country_code(region)
    country = pycountry.countries.get(alpha_2=code) if code else None
    if country:
        names.extend(v for v in (country.name, getattr(country, "official_name", None),
                                 getattr(country, "common_name", None), country.alpha_3) if v)
    if region["id"] == "custom":
        names.append(region["label"])
    return [name for name in dict.fromkeys(str(n).strip() for n in names) if name]


def _region_text_excluded(text: str, region: dict) -> bool:
    """Common “not available in <region>” / “excluding <region>” statements."""
    folded = fold(text)
    for name in _region_names(region):
        needle = re.escape(fold(name))
        if not needle:
            continue
        if re.search(r"\bnot\s+(?:currently\s+)?(?:available|open|eligible|offered|supported|hiring)\s+"
                     r"(?:(?:to|for|in|within)\s+)?(?:(?:candidates?|applicants?|residents?|people)\s+)?"
                     r"(?:(?:in|from|within)\s+)?(?:the\s+)?" + needle + r"\b", folded):
            return True
        if re.search(r"\b(?:except|excluding)\s+(?:(?:candidates?|applicants?|residents?|people)\s+)?"
                     r"(?:(?:in|from|within)\s+)?(?:the\s+)?" + needle + r"\b", folded):
            return True
        for match in re.finditer(r"\boutside(?:\s+of)?\s+(?:the\s+)?" + needle + r"\b", folded):
            before = folded[max(0, match.start() - 40):match.start()]
            # "not available outside the US" restricts the job to the US, so the US is eligible.
            if not re.search(r"not\s+(?:currently\s+)?available\s*$", before):
                return True
    return False


def location_options(location: str, region=None, country_hint: str | None = None) -> list[str]:
    """Location display segments that qualify for ``region`` (empty means not eligible)."""
    region = normalize_region(region)
    text = re.sub(r"\s+", " ", str(location or "")).strip()
    if region["id"] == "global":
        return [text or "Anywhere"]
    if region["id"] == "ca":
        # Reuse the app's battle-tested Canadian place logic (shared province codes,
        # "Ontario, CA", "London, Ontario" vs London UK, and exclusion statements).
        from . import core as core_module
        options = core_module.canadian_options(text)
        if options:
            return options
        hint = str(country_hint or "").strip().upper()
        if hint in {"CA", "CAN", "CANADA"} and not core_module.foreign_location(text):
            return [(text + " · Canada").strip(" ·")] if text else []
        return []
    pattern = _alias_pattern(region["id"], tuple(region["terms"]))
    looks_relevant = bool((pattern and (pattern.search(fold(text)) or pattern.search(text)))
                          or re.search(r"\bremote\b|\banywhere\b|work from home|\bwfh\b", text, re.I))
    if looks_relevant and _region_text_excluded(text, region):
        return []
    selected: list[str] = []
    for part in _segments(text):
        if pattern and (pattern.search(fold(part)) or pattern.search(part)):
            selected.append(part)
        elif region["include_remote"] and re.search(r"\bremote\b|\banywhere\b|work from home|\bwfh\b", part, re.I):
            if not _mentions_other_country(part, region):
                selected.append(f"Remote — {region['label']}")
    hint = str(country_hint or "").strip().upper()
    if not selected and hint and country_code(region) and hint in {country_code(region), region["id"].upper()}:
        if not _mentions_other_country(text, region):
            selected.append((text + " · " + region["label"]).strip(" ·"))
    return list(OrderedDict.fromkeys(selected))


def matches(location: str, region=None, country_hint: str | None = None) -> bool:
    return bool(location_options(location, region, country_hint))


def source_filters(source: dict, region=None) -> dict:
    """Return a copy of a source with region-specific request settings applied."""
    region = normalize_region(region)
    source = dict(source)
    source["search_region"] = region
    kind = source.get("type")
    if kind == "workday" and source.get("country"):
        if region["id"] == "global" or region["id"] == "custom":
            source.pop("country", None)
        else:
            source["country"] = country_name(region) or source.get("country")
    if kind == "smartrecruiters":
        code = country_code(region)
        if code:
            source["country_code"] = code.lower()
        else:
            source.pop("country_code", None)
    return source


def catalog() -> list[dict]:
    """Countries for a region picker; common destinations first."""
    common = ["ca", "us", "gb", "au", "de", "fr", "nl", "ie", "in", "sg", "nz", "jp"]
    countries = {c.alpha_2.casefold(): c.name for c in pycountry.countries}
    rows = [{"id": "global", "label": "Anywhere (global)"}]
    for code in common:
        if code in countries:
            rows.append({"id": code, "label": countries.pop(code)})
    rows.append({"id": "custom", "label": "Custom region…"})
    rows.extend({"id": code, "label": name} for code, name in sorted(countries.items(), key=lambda item: item[1]))
    return rows
