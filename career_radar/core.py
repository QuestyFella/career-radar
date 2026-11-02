"""Career Radar: conservative matching, persistent job state, and real deadlines.

The match score ranks interests, not a promise of eligibility. Application state is
never inferred from clicking a link. All timestamps are UTC, display in Toronto.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import Counter
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from dateutil import parser as dateparser
import pycountry
from . import regions
from .url_safety import canonical_job_url
from .deadlines import parse_published_deadline, parse_structured_deadline

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = None  # resolved per call from the data directory (config.db_path())
# Wall-clock budget for one collection; sources not started in time rotate to the next run.
COLLECTION_BUDGET_MINUTES = 40
MAX_COLLECTION_MINUTES = 180  # largest budget the settings allow
TORONTO = ZoneInfo("America/Toronto")
STATES = {"new", "saved", "applied", "dismissed"}
IDENTITY_POLICY_VERSION = 2
IDENTITY_POLICY = f"exact-v{IDENTITY_POLICY_VERSION}"
CITY_NAMES = ("Toronto", "Ottawa", "Montréal", "Montreal", "Vancouver", "Waterloo", "Kitchener", "Mississauga", "Guelph", "Calgary", "Edmonton", "Winnipeg", "Halifax", "Victoria", "Burnaby", "Markham", "Brampton", "Oakville", "Québec", "Quebec", "Sherbrooke", "Laval", "Saskatoon", "Regina", "Fredericton", "Moncton", "Kanata", "Burlington", "Richmond Hill", "Cambridge", "St. John's")
PROVINCES = r"ontario|qu[eé]bec|british columbia|alberta|manitoba|saskatchewan|nova scotia|new brunswick|newfoundland(?: and labrador)?|prince edward island|yukon|nunavut|northwest territories"
CANADA = re.compile(r"\b(?i:canada|canadian|" + PROVINCES + r")\b|(?<![A-Z]{2}-)\bCAN\b|,\s*(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b")
# Province codes shared with other places (NL Netherlands, PE Peru/Pernambuco, SK Slovakia)
# count only beside a city of that province; "NZ-CAN-..." is a region code, not Canada.
_SHARED_CODE = re.compile(r",\s*(?:NL|PE|SK)\b")
_SHARED_CODE_CITIES = re.compile(r"\b(?:St\.?\s*John['’]s|Mount Pearl|Corner Brook|Gander|Conception Bay South|Charlottetown|"
                                 r"Summerside|Saskatoon|Regina|Prince Albert|Moose Jaw|Swift Current|Yorkton|"
                                 r"North Battleford|Estevan|Lloydminster|Esterhazy)\b", re.I)
# Bare "Ontario" in a list of these cities is Ontario, California.
_US_NEAR_ONTARIO = re.compile(r"\b(?:San Luis Obispo|Laguna Hills|San Diego|Los Angeles|Irvine|Riverside|San Bernardino|"
                              r"Rancho Cucamonga|Pomona|Folsom|Sacramento|Fresno|Portland|Seattle|Phoenix|Las Vegas|"
                              r"San Francisco|San Jose|Oakland|Palo Alto)\b", re.I)
# A Canadian token inside a prohibition is never eligibility evidence, whether
# the Canadian group is the subject ("Canadian residents cannot apply") or the
# object of a refusal ("not accepting Canadian applicants"). A prohibition of
# non-Canadians, or of people not located in Canada, is the opposite statement.
_NOT_NON = r"(?<!non-)(?<!non\s)"
_PEOPLE = (r"(?:residents?|citizens?|nationals?|candidates?|applicants?|people|persons|individuals|workers|talent|"
           r"contractors|employees|students|hires?)")
_IN_CANADA = r"(?:(?:who\s+(?:are|is)\s+)?(?:located|based|living|residing)\s+)?(?:in|from|of|within)\s+Canada\b"
_CANADIAN_GROUP = (_NOT_NON + r"\bCanadians\b|" + _NOT_NON + r"\bCanadian\s+" + _PEOPLE + r"\b|\bCanada[- ]based\s+"
                   + _PEOPLE + r"\b")
_CANADIAN_SUBJECT = r"(?:" + _CANADIAN_GROUP + r"|\b(?:" + _PEOPLE + r"|anyone|those|applications?)\s+" + _IN_CANADA + ")"
_CANADIAN_OBJECT = (r"(?:" + _CANADIAN_GROUP + r"|\b(?:" + _PEOPLE + r"|anyone|applications?)\s+" + _IN_CANADA
                    + r"|\b(?:in|from|within)\s+Canada\b)")
_HEDGE = r"(?:\s*,?\s*(?:unfortunately|regrettably|sadly|currently|presently|however|at\s+(?:this\s+time|present))\s*,?)*"
_SUBJECT_PROHIBITION = (
    _CANADIAN_SUBJECT + _HEDGE + r"\s+(?:(?:(?:is|are|will\s+be|would\s+be)\s+)?(?:(?:currently|presently)\s+)?"
    r"(?:not\s+(?:currently\s+)?(?:eligible|accepted|considered|permitted|allowed|supported|authorized|available|open|welcome)"
    r"|ineligible|excluded|prohibited|barred|disqualified)\b"
    r"|(?:cannot|can\s+not|can['’]t|may\s+not|must\s+not|need\s+not|should\s+not|will\s+not|won['’]t"
    r"|(?:(?:is|are)\s+)?(?:not\s+able|unable)\s+to)\s+(?:currently\s+)?"
    r"(?:apply|work|participate|be\s+(?:considered|hired|employed|accepted|eligible|engaged))\b)")
_ACTION_PROHIBITION = (
    r"\b(?:(?:not|never|no\s+longer)\s+(?:currently\s+)?(?:accepting|considering|hiring|recruiting|employing|onboarding)"
    r"|(?:won['’]t|(?:do|does|did|will|can|may)\s*(?:not|n['’]t))\s+(?:currently\s+)?(?:hire|employ|accept|consider|recruit|onboard)"
    r"|(?:cannot|can['’]t|can\s+not|(?:not\s+able|unable)\s+to)\s+(?:currently\s+)?(?:hire|employ|accept|consider|recruit|onboard))"
    r"\s+(?:applications?\s+from\s+)?" + _CANADIAN_OBJECT
    + r"|\bno\s+(?:applications?\s+from\s+)?(?:" + _CANADIAN_GROUP + r"|\b" + _PEOPLE + r"\s+" + _IN_CANADA + ")")
CANADA_EXCLUDED = re.compile(
    r"\b(?:except|excluding|outside(?:\s+of)?)\s+(?:Canada|Canadian\s+(?:residents?|candidates?|applicants?))\b"
    r"|\bnot(?:\s+(?:currently\s+)?(?:available|eligible|permitted|authorized|supported|hiring|open))?"
    r"\s+(?:(?:to|for)\s+)?(?:(?:candidates?|applicants?|residents?|people)\s+)?"
    r"(?:(?:in|for|within|of)\s+)?(?:Canada|Canadian\s+(?:residents?|candidates?|applicants?))\b"
    r"|" + _NOT_NON + r"\b(?:Canada|Canadian)(?:\s+(?:residents?|candidates?|applicants?))?[\s,:(\[]*(?:(?:is|are)\s+)?"
    r"(?:excluded|ineligible|not\s+(?:currently\s+)?(?:eligible|available|permitted|supported|authorized))\b"
    r"|" + _SUBJECT_PROHIBITION + r"|" + _ACTION_PROHIBITION, re.I)
UNAMBIGUOUS_CITIES = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in CITY_NAMES if c not in {"Victoria", "Cambridge", "Burlington", "Richmond Hill"}) + r")\b", re.I)
CITIES = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in CITY_NAMES) + r")\b", re.I)
US_STATES = "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|district of columbia|puerto rico"
# ISO names avoid treating an unlisted foreign country as a Canadian city
# suffix. Historical/common short names supplement the maintained dataset.
_country_names = {"South Korea", "North Korea", "Russia", "Taiwan", "Vietnam", "Turkey", "Czech Republic", "Ivory Coast", "Laos", "Syria", "Iran", "Bolivia", "Venezuela", "Moldova", "Tanzania", "Brunei", "Palestine"}
for _country in pycountry.countries:
    if _country.alpha_2 != "CA":
        for _field in ("name", "official_name", "common_name"):
            if _name := getattr(_country, _field, None):
                _country_names.add(_name)
                _country_names.add(unicodedata.normalize("NFKD", _name).encode("ascii", "ignore").decode())
FOREIGN_COUNTRY_NAMES = "|".join(re.escape(name) for name in sorted(_country_names, key=len, reverse=True))
FOREIGN = re.compile(r"(?<!\w)(?i:united states|united kingdom|USA|U\.S\.?|US|UK|england|germany|france|india|singapore|australia|new zealand|mexico|US[- ]only|washington|california|texas|new york|illinois|massachusetts|pennsylvania|oregon|ohio|michigan|colorado|florida|arizona|netherlands|ireland|poland|spain|portugal|china|japan|austria|switzerland|sweden|denmark|" + US_STATES + "|" + FOREIGN_COUNTRY_NAMES + r")(?!\w)|,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|PR)\b")
SENIOR = re.compile(r"\b(?:senior|sr\.?|staff|principal|director|head of|vice president|vp|chief|architect)\b|\b(?:engineer|developer)\s+(?:III|IV|V|[3-9])\b|\bteam lead\b", re.I)
# Short tokens are word-bounded: bare "UI" matched Guide/Building/Recruiter, "product" matched
# Production, "test" matched Contest, "mobile" matched Automobile.
FIELDS = re.compile(r"software|develop(?:er|ment)|engineer|data|analytics?|machine learning|artificial intelligence|\bAI\b|\bML\b|firmware|embedded|avionics|robotics|cyber|security|devops|cloud|site reliability|\bSRE\b|\bQA\b|quality assurance|\btest(?:s|ing|ers?|eur|euse)?\b|technical support|IT support|help.?desk|information technology|information systems|\bsystems?\b|network|\bproducts?\b|production support|programmer|comput(?:er|ing)|research assistant|quantitative|business analyst|\bUX\b|\bUI\b|web|\bmobile\b|technology|linux|unix|kernel|circuit|silicon|\bFPGA\b|\bASIC\b|\bPCB\b|\bVLSI\b|semiconductor|d[eé]veloppeu[rs]|logiciel|informatique|analyste|donn[eé]es|ing[eé]nieur|assurance qualit[eé]", re.I)
CS_TEXT = re.compile(r"computer science|computer engineering|software engineering|information technology|computing", re.I)
SPACE = re.compile(r"aerospace|spacecraft|satellite|rocketry|rocket|flight software|avionics|Kepler|Telesat|MDA Space|GHGSat|NordSpace|Magellan|Pratt . Whitney|Bombardier|CAE\b|Honeywell", re.I)
EMBEDDED = re.compile(r"embedded|firmware|robotics|avionics|flight software|flight control|\bGNC\b|\bRTOS\b|FPGA|autonomous|controls engineer", re.I)
LEVEL_PATTERNS = [("co_op", r"\bco[ -]?op\b|\bcoop\b"), ("internship", r"\bintern(?:ship)?\b|\bstagiaire\b|\bstage\b|\bstudent\b"), ("new_grad", r"new[ -]?grad|new graduate|recent graduate|graduate program|graduate recruitment|early career|university grad|campus hire|202[6-9] graduate"), ("entry_level", r"\bjunior\b|\bjr\.?\b|entry[ -]level|\bassociate\b|\b(?:engineer|developer|analyst|specialist)\s+(?:I|1)\b")]
EARLY_LEVELS = tuple(level for level, _ in LEVEL_PATTERNS)
ALL_LEVELS = EARLY_LEVELS + ("mid_level", "senior_level")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def fold(text):
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def canonical_url(value):
    """Shared job-link validation and conservative canonical identity."""
    return canonical_job_url(value)


def foreign_location(location):
    # A country-named Canadian town (Jordan, Ontario) is not an overseas job.
    # A foreign country after the province (Toronto, Ontario, Belgium) is.
    text = str(location or "")
    if "," in text:
        remainder = text.split(",", 1)[1]
        if CANADA.search("," + remainder) and not FOREIGN.search(remainder):
            return False
    return bool(FOREIGN.search(text))


def _canada_signal(part):
    """This location segment names a Canadian place (see _SHARED_CODE / _US_NEAR_ONTARIO)."""
    if UNAMBIGUOUS_CITIES.search(part):
        return True
    signals = [m.group() for m in CANADA.finditer(part)]
    strong = [s for s in signals if not _SHARED_CODE.fullmatch(s)]
    if strong:
        return not (all(s.casefold() == "ontario" for s in strong) and _US_NEAR_ONTARIO.search(part))
    return bool(signals and _SHARED_CODE_CITIES.search(part))


def canadian_options(location):
    text = str(location or "").strip()
    if CANADA_EXCLUDED.search(text):
        return []
    selected = []
    segments = re.split(r"\s*(?:\||;|\n| / |\bor\b)\s*", text)
    for part in segments:
        part = part.strip()
        if foreign_location(part):
            if re.search(r"\bcanada\b", part, re.I):
                part = "Remote — Canada" if re.search(r"remote", part, re.I) else "Canada"
            else:
                continue
        elif not _canada_signal(part):
            continue
        # Strip display wrappers only after checking the original geography.
        part = part.strip(" ()[]")
        if re.match(r"remote\b", text, re.I) and part.lower() in {"canada", "can"}:
            part = "Remote — Canada"
        if part and part not in selected:
            selected.append(part)
    return selected


def canadian_location(location):
    return bool(canadian_options(location))


def parsed_date(value, *, end_of_day=False):
    if not value:
        return None
    try:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            d = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(d, time(23, 59, 59) if end_of_day else time(), TORONTO).astimezone(timezone.utc)
        dt = dateparser.isoparse(text)
        return (dt.replace(tzinfo=TORONTO) if dt.tzinfo is None else dt).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _deadline_details(raw):
    """Resolve source evidence with one clock/DST parser; keep caveats separate."""
    source = canonical_url(raw.get("deadline_source_url")) or canonical_url(raw.get("url"))
    value = raw.get("deadline")
    evidence = str(raw.get("deadline_evidence") or "").strip()
    structured = parse_structured_deadline(value) if evidence else None
    published = (parse_published_deadline(str(raw.get("description") or ""))
                 or parse_published_deadline(evidence))
    parsed = structured
    # Explicit cutoff prose (including RBC's prior-day note) must be evaluated
    # even alongside validThrough metadata. A consistent structured clock may
    # refine a plain date, but cannot erase a published clock or uncertainty.
    if published and (not structured or published.uncertainty or "T" in published.value
                      or "T" not in structured.value or published.value != structured.value[:10]):
        parsed, evidence = published, published.evidence
    if parsed:
        date_only = "T" not in parsed.value
        dt = parsed_date(parsed.value, end_of_day=date_only)
        if dt:
            return iso(dt), "date" if date_only else "datetime", evidence[:500], source, parsed.uncertainty
    return None, None, None, None, None


def extract_deadline(raw):
    """Preserve the existing four-field API for callers outside normalization."""
    return _deadline_details(raw)[:4]


# Abbreviated credentials are case-sensitive, so "ms" and "Ms." are not degrees. A bare
# "MS" counts only in degree context, so "MS Office", "MS-SQL" and "LC-MS/MS" are not
# credentials.
_MS_DEGREE = (r"(?<!-)(?<!MS/)MS(?!/MS\b)(?=(?i:\s*(?:in\b|degree|,|\(|or\b|and\b|students?\b|candidates?\b|program|"
              r"required\b|preferred\b|level\b|graduates?\b|holders?\b))|/\s*(?:PhD|[BM][A-Z]))")
_ADVANCED_ABBREVIATION = rf"(?-i:{_MS_DEGREE}|MSc|MSCS|MSEE|MEng|MASc|MMath|MBA)"
_BACHELOR_ABBREVIATION = r"(?-i:BS|BSc|BSCS|BSEE|BEng|BASc|BMath|BCom|BBA)"
_ADVANCED_DEGREE = (r"\b(?:PhD|doctorate|doctoral(?: degree)?|master['’]?s(?: degree)?|"
                    r"master\s+(?:degree|of\s+(?:science|engineering|applied\s+science|computer\s+science|mathematics|arts|business))|"
                    rf"graduate degree|postgraduate degree|{_ADVANCED_ABBREVIATION})\b")
_BACHELOR_DEGREE = (r"\b(?:bachelor(?:['’]?s)?(?: degree)?|undergraduate(?: degree)?|"
                    rf"(?:university|college|post[-\s]?secondary)\s+(?:degree|diploma)|{_BACHELOR_ABBREVIATION})\b")
_DEGREE_MENTION = re.compile(rf"(?P<bachelor>{_BACHELOR_DEGREE})|(?P<advanced>{_ADVANCED_DEGREE})", re.I)
# Dotted credentials ("B.Sc.", "M.A.Sc.") are not sentence boundaries, except that a
# final period before a capitalized word may still end the sentence ("a B.S. Our team").
_DOTTED_CREDENTIAL = re.compile(r"\b([BM])\.\s?(A\.\s?Sc|Sc|S|Eng)(?:\.(?=\s*[,;:/()]|\s+[a-z]))?(?![A-Za-z])")
# The separator directly before the next credential. Any text before it is the
# previous credential's field of study, which may itself contain commas, 'or'
# and 'and' ("in Electrical, Computer, or Software Engineering").
_DEGREE_SEPARATOR = re.compile(
    r"(?P<comma>,)?\s*(?:\b(?P<conj>and/or|or|and|as\s+well\s+as|plus|along\s+with|together\s+with|in\s+addition\s+to)\b"
    r"|(?P<slash>/))?\s*(?:\b(?:an?|the)\s+)?$", re.I)
_DEGREE_SUBJECT = re.compile(r"\s*(?:\(|(?:in|from)\b|or\s+(?:equivalent|higher|above)\b)", re.I)
# A requirement verb or a different noun phrase ends the credential's subject.
_SUBJECT_BREAK = re.compile(r"\b(?:required|requires?|requiring|must|mandatory|essential|preferred|optional|with|but|also|"
                            r"is|are|be|you|we|will|would|should|may|can|candidates?|applicants?|students?|mentor\w*)\b", re.I)
# Quantifiers that make an enumeration joint: "Both degrees are required: A, B".
_JOINT_DEGREES = re.compile(
    r"\bboth\s+(?:(?:an?|the)\s+)?$"
    r"|\b(?:both|all|each|two|three|four)\s+(?:of\s+)?(?:(?:these|those|the\s+following|the)\s+)?"
    r"(?:degrees|credentials|qualifications|diplomas)\b"
    r"|\b(?:all|each)\s+of\s+(?:the\s+following|these|those)\b", re.I)


def _degree_chains(clause):
    """Group credential mentions joined only by list separators."""
    chains = []
    for mention in _DEGREE_MENTION.finditer(clause):
        if chains:
            items, joins = chains[-1]
            gap = clause[items[-1].end():mention.start()]
            separator = _DEGREE_SEPARATOR.search(gap)
            subject = gap[:separator.start()]
            conj = (separator.group("conj") or "").lower()
            join = (("or" if conj in {"or", "and/or"} else "and") if conj
                    else "slash" if separator.group("slash") else "comma" if separator.group("comma") else None)
            if join and len(subject) <= 200 and (not subject.strip() or (
                    _DEGREE_SUBJECT.match(subject) and not _SUBJECT_BREAK.search(subject))):
                items.append(mention)
                joins.append(join)
                continue
        chains.append(([mention], []))
    return chains


def _bachelor_route(items, joins, joint):
    """Whether one disjunct of the enumeration needs no advanced credential."""
    resolved = []
    for index, join in enumerate(joins):
        if join in {"comma", "slash"}:
            # A serial-list comma takes the list's explicit conjunction; only an
            # unquantified list without one keeps the earlier alternative reading.
            following = next((j for j in joins[index + 1:] if j in {"or", "and"}), None)
            preceding = next((j for j in reversed(joins[:index]) if j in {"or", "and"}), None)
            join = "and" if joint else "or" if join == "slash" else following or preceding or "or"
        resolved.append(join)
    route = [items[0]]
    for item, join in zip(items[1:], resolved):
        if join == "or":
            if all(m.group("bachelor") for m in route):
                return True
            route = [item]
        else:
            route.append(item)
    return all(m.group("bachelor") for m in route)


def _credential_text(description):
    """Undot credential abbreviations so their periods are not sentence boundaries."""
    text = re.sub(r"\bPh\.?D\.?", "PhD", description, flags=re.I)

    def undot(match):
        degree = re.sub(r"[.\s]", "", match.group(2))
        # "M.S." is unambiguous, unlike a bare "MS", so it becomes "MSc".
        return match.group(1) + ("Sc" if degree == "S" else degree)
    return _DOTTED_CREDENTIAL.sub(undot, text)


def advanced_degree_required(description):
    # Alternatives apply only to the degree enumeration containing this
    # credential, not to an unrelated undergraduate mention in the clause.
    text = _credential_text(description)
    # Abbreviations inside a field-of-study list are not sentence boundaries.
    text = re.sub(r"\b(?i:e\.\s?g|i\.\s?e)\.|\b(?i:etc)\.(?=\s*[,;:)]|\s+[a-z])",
                  lambda m: m.group().replace(".", ""), text)
    for clause in re.split(r"[.;\n!?]", text):
        alternatives = set()
        for items, joins in _degree_chains(clause):
            if len(items) > 1 and _bachelor_route(items, joins, bool(_JOINT_DEGREES.search(clause[:items[0].start()]))):
                alternatives.update(m.start() for m in items)
        for degree in _DEGREE_MENTION.finditer(clause):
            if not degree.group("advanced") or degree.start() in alternatives:
                continue
            before = clause[max(0, degree.start() - 100):degree.start()]
            after = clause[degree.end():degree.end() + 160]
            local = before + degree.group() + after
            if re.match(r"\s+(?:is\s+)?(?:not\s+required|optional|preferred)\b", after, re.I):
                continue
            if re.search(r"\b(?:preferred|optional)(?:\s+qualifications?)?\s*:\s*(?:an?\s+)?$", before, re.I):
                continue
            if re.search(r"\b(?:work|collaborat(?:e|ing)|partner|interact)\s+(?:closely\s+)?(?:with|alongside)\s*$", before, re.I):
                continue
            if re.search(r"\b(?:not|never)\s+requires?\s+(?:an?\s+)?$", before, re.I):
                continue
            if re.search(r"\b(?:requires?|required|mandatory|essential|must|pursuing|enrolled|ongoing|currently|looking for|recently completed)\b|\bminimum\s+(?:qualifications?|education|requirements?)\b", local, re.I):
                return True
    return False


def us_citizenship_required(description):
    text = re.sub(r"\bU\.S\.(?:A\.)?", "US", description, flags=re.I)
    country = r"(?:US|USA|United States(?: of America)?|American)"
    credential = rf"\b(?:{country}\s+(?:citizenship|citizens?|nationals?)|(?:citizenship|citizens?|nationals?)\s+of\s+(?:the\s+)?{country})\b"
    for match in re.finditer(credential, text, re.I):
        before = re.split(r"[.;\n]", text[max(0, match.start() - 120):match.start()])[-1]
        after = re.split(r"[.;\n]", text[match.end():match.end() + 120])[0]
        if re.search(r"\bnon[-\s]*$", before, re.I):
            if re.match(r"\s*(?:(?:is|are)\s+)?(?:not\s+eligible|ineligible|cannot\s+apply)\b", after, re.I):
                return True
            continue
        if re.search(r"\bCanadian(?:\s+(?:citizenship|citizens?|permanent\s+residen(?:cy|ce|ts?)))?\s+or\s*$", before, re.I):
            continue
        if re.search(r"\b(?:no|without|(?:not|never)\s+(?:require|requires|need|needs))\s*$", before, re.I):
            continue
        if re.match(r"\s*(?:(?:is|are)\s+)?not\s+(?:required|needed|mandatory)\b", after, re.I):
            continue
        if re.match(r"\s*or\s+(?:an?\s+)?Canadian\s+(?:citizenship|citizens?|permanent\s+residen(?:cy|ce|ts?))\b", after, re.I):
            continue
        prefix = re.search(r"\b(?:must|required\s+to|need(?:s)?\s+to|has\s+to)\s+(?:be|hold|have|possess|obtain)\s+(?:an?\s+)?$|\b(?:only|requires?|requiring)\s*$|\b(?:minimum|required|basic|essential)\s+(?:qualifications?|requirements?)\s*:\s*$", before, re.I)
        postfix = re.match(r"\s*(?:(?:is|are)\s+)?(?:required|mandatory|essential|only)\b", after, re.I)
        if prefix or postfix:
            return True
    return False


_GENERIC_TITLE = re.compile(r"analyst|associate|research|trainee|graduate", re.I)


# Roles that share words with technical titles but are never CS/AI/embedded/aerospace work.
_NON_TECH_ALWAYS = re.compile(r"\b(?:data\s+entry|recruiters?|AI\s+(?:trainers?|tutors?|writers?|raters?)|"
                              r"cashiers?|crane|forklift|sales\s+(?:associates?|representatives?|reps?|consultants?|advisors?))\b", re.I)
# Other disciplines and occupations ("Structural Engineer", "Security Guard", "Business
# Development"); kept only when the title also names computing or aerospace work.
_NON_TECH_TITLE = re.compile(
    r"\b(?:civil|structural|geotechnical|g[eé]otechni\w*|geomatics|geological|environmental|water\s+resources|"
    r"wastewater|stormwater|municipal|transportation|highway|roadway|building|construction|(?<!data )mining|"
    r"petroleum|metallurgical|chemical|HVAC|plumbing|piping|fire\s+protection|coastal|hydrotechnical|"
    r"g[eé]nie\s+civil|b[aâ]timent|chimique|marketing|talent\s+acquisition|(?:business|sales)\s+development|"
    r"security\s+(?:guards?|officers?|screening|patrol)|loss\s+prevention|financial\s+security|auto\s+mobile|"
    r"(?:learning|organi[sz]ational|fund|land|real\s+estate|community|economic|child|youth|donor|curriculum)"
    r"\s+(?:(?:and|&)\s+)?development)\b", re.I)
_STRONG_TECH = re.compile(
    r"software|logiciel|developer|d[eé]veloppeu|programm(?:er|eur)|firmware|embedded|comput(?:er|ing)|informatique|"
    r"cyber|devops|cloud|machine\s+learning|data\s+(?:scien\w*|engineer\w*|analy\w*)|analytics|robotics|avionics|"
    r"information\s+(?:technology|security|systems)|\bweb\b|full.?stack|front.?end|back.?end|aerospace|aircraft|spacecraft|satellite|"
    r"rocket\w*|propulsion|flight|algorithm\w*|GNSS|\bGPS\b|navigation|autonom\w*|simulation|sensors?\b|silicon|"
    r"systems?\s+analyst|(?-i:\b(?:IT|AI|ML|GIS)\b)", re.I)


def _non_technical_title(title):
    """Titles that are never technical whatever their description says."""
    return bool(_NON_TECH_ALWAYS.search(title)
                or (_NON_TECH_TITLE.search(title) and not _STRONG_TECH.search(title)))


def _title_excluded(title, all_levels=False):
    """Title-only exclusions shared by infer_level and the cheap fetch-time gate.

    With ``all_levels`` the seniority and advanced-degree exclusions are lifted;
    non-technical roles are still rejected.
    """
    if _non_technical_title(title):
        return True
    if all_levels:
        return False
    if SENIOR.search(title):
        return True
    if re.search(r"\b(?:Ph\.?D|doctoral|master['’]?s|masters)\b", title, re.I) and not re.search(r"bachelor|undergrad", title, re.I):
        return True
    return bool(re.search(r"\b(?:manager|lead)\b", title, re.I) and not re.search(r"intern|co.?op|junior|associate|new.?grad", title, re.I))


def title_could_match(title, all_levels=False):
    """False only when infer_level rejects this title whatever the description says.

    Adapters use it to skip detail requests for postings that can never match."""
    title = str(title or "")
    return not _title_excluded(title, all_levels) and bool(FIELDS.search(title) or _GENERIC_TITLE.search(title))


def technical_title(title, all_levels=False):
    """A title that names technical work by itself (no "Associate"/"Analyst"-only titles).

    Source discovery uses it: a board is worth polling only if it lists such roles."""
    title = str(title or "")
    return not _title_excluded(title, all_levels) and bool(FIELDS.search(title))


def detail_priority(title):
    """Sort key for capped detail fetching: early-career technical titles first."""
    title = str(title or "")
    early = any(re.search(pattern, title, re.I) for _, pattern in LEVEL_PATTERNS)
    return (0 if early else 1, 0 if FIELDS.search(title) else 1)


def infer_level(title, description, hint=None, all_levels=False):
    if _title_excluded(title, all_levels):
        return None
    if not (FIELDS.search(title) or (_GENERIC_TITLE.search(title) and CS_TEXT.search(description))):
        return None
    if not all_levels and advanced_degree_required(description):
        return None
    if us_citizenship_required(description):
        return None
    min_years = []
    # Normalize number words only for requirement matching, never source text.
    words = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    numbers = {word: str(number) for number, word in enumerate(words)}
    experience_text = re.sub(r"\b(?:" + "|".join(words) + r")\b", lambda m: numbers[m.group().lower()], description, flags=re.I)
    experience_text = re.sub(r"\b(\d{1,2})\s*\(\s*\1\s*\)", r"\1", experience_text)
    experience_text = re.sub(r"\b(\d{1,2})[- ]plus\b", r"\1+", experience_text, flags=re.I)
    # Up to six words may sit between "years" and "experience", including comma lists
    # ("4+ years of software deployment, infrastructure, or systems integration experience").
    experience = r"\b(\d{1,2})(?:\s*(?:[-–]|to)\s*\d{1,2}|\s+or\s+more)?\s*\+?\s*(?:years|yrs)['’]?(?:\s+of)?((?:,?\s+[a-z+/\-]+){0,6})\s+experience\b"
    for m in re.finditer(experience, experience_text, re.I):
        # Years of schooling ("3 years of post-secondary education, plus experience with") are not work minimums.
        if re.search(r"\b(?:education|post-?secondary|stud(?:y|ies)|degree|school|universit\w*|college|diploma|undergrad\w*|coursework|academic)\b",
                     m.group(2), re.I):
            continue
        before = re.split(r"[.;\n]", experience_text[max(0, m.start() - 65):m.start()])[-1]
        after = re.split(r"[.;\n]", experience_text[m.end():m.end() + 60])[0]
        employer_history = re.search(r"\b(?:we|(?:our|the)\s+(?:company|business|team))\s+(?:have|has|bring|brings|offer|offers)(?:\s+(?:over|nearly|about|around|more\s+than|at\s+least|a\s+combined))?\s*$", before, re.I)
        if employer_history and not re.match(r"\s*(?:is\s+)?(?:required|mandatory|minimum)\b", after, re.I):
            continue
        if re.match(r"[\s(]*(?:is\s+)?not\s+(?:required|necessary|needed)\b", after, re.I):
            continue
        # "Up to three years" and "less than 4 years" cap experience; they are not minimums.
        if re.search(r"\b(?:up\s+to|less\s+than|fewer\s+than|under|no\s+more\s+than|at\s+most|maximum(?:\s+of)?)\s*$", before, re.I):
            continue
        context = (before + " " + after).lower()
        mandatory = re.search(r"minimum|at least|must|require", context)
        preferred = re.search(r"preferred|nice.to.have|asset|bonus", context)
        if mandatory or not preferred:
            min_years.append(int(m.group(1)))
    if min_years and max(min_years) > 2 and not all_levels:
        return None
    for level, pattern in LEVEL_PATTERNS:
        if re.search(pattern, title, re.I):
            if level == "entry_level" and re.search(LEVEL_PATTERNS[2][1], description, re.I):
                return "new_grad"
            return level
    if re.search(LEVEL_PATTERNS[2][1], description, re.I):
        return "new_grad"
    low_experience = bool(min_years) and max(min_years) <= 2
    if low_experience or re.search(r"no (?:prior )?experience (?:required|necessary)|entry.level|recent graduate", description, re.I):
        return "entry_level"
    if hint in {"internship", "co_op", "new_grad", "entry_level"}:
        return hint
    if all_levels:
        # Past the early-career rules: classify the rest instead of dropping it.
        if (SENIOR.search(title) or (min_years and max(min_years) >= 5)
                or re.search(r"\b(?:manager|director|head of|vice president|vp|chief)\b", title, re.I)):
            return "senior_level"
        if hint == "senior_level":
            return hint
        return "mid_level"
    return None


def category_for(title):
    for category, pattern in [
        ("Embedded & robotics", r"embedded|firmware|avionics|robotics|hardware|electrical|electronics|GNC|flight"),
        ("Data & AI", r"data|machine learning|artificial intelligence|\bAI\b|\bML\b|quantitative|analytics"),
        ("Security", r"cyber|security"),
        ("QA & testing", r"\bQA\b|quality assurance|\btest(?:s|ing|ers?|eur|euse)?\b"),
        ("Cloud & DevOps", r"devops|cloud|infrastructure|site reliability|\bSRE\b|platform engineer"),
        ("IT & support", r"support|help.?desk|information technology|\bIT\b|network|systems admin"),
        ("Product & analysis", r"\bproducts?\b|business analyst|\bUX\b|\bUI\b|project|program analyst"),
        ("Research & other tech", r"research|scientist|mechanical|propulsion|aerospace"),
    ]:
        if re.search(pattern, title, re.I):
            return category
    return "Software & web"


def _deadline_caveat(uncertainty):
    explanations = {
        "missing_timezone": "has no published timezone",
        "unknown_timezone": "has an unknown or unrecognized timezone",
        "invalid_clock": "contains an invalid published clock",
        "ambiguous_local_time": "is ambiguous at the daylight-saving transition",
        "nonexistent_local_time": "does not exist at the daylight-saving transition",
    }
    reason = explanations.get(uncertainty, "is not sufficiently specified")
    return f"The deadline time {reason}; confirm the exact cut-off on the source. Only its calendar date is shown."


def normalize_job(raw, now=None, *, region=None, levels=None):
    now = now or utcnow()
    region = regions.normalize_region(region)
    allowed_levels = tuple(levels) if levels else EARLY_LEVELS
    include_all = bool(set(allowed_levels) & {"mid_level", "senior_level"})
    title, company = str(raw.get("title") or "").strip(), str(raw.get("company") or "").strip()
    location = str(raw.get("location") or "").strip()
    url = canonical_url(raw.get("url"))
    if not title or not company or not url:
        return None
    if regions.is_canada(region):
        if CANADA_EXCLUDED.search(location):
            return None
        options = canadian_options(location)
        if options:
            location = "; ".join(options)
        elif str(raw.get("country_hint", "")).upper() in {"CA", "CAN", "CANADA"} and not foreign_location(location):
            location = (location + " · Canada").strip(" ·")
        else:
            return None
    else:
        options = regions.location_options(location, region, raw.get("country_hint"))
        if not options:
            return None
        location = "; ".join(options)
    description = re.sub(r"\s+", " ", str(raw.get("description") or "")).strip()[:45000]
    degree_hint = "; ".join(str(d) for d in (raw.get("degrees") or []))
    listed = re.search(r"listed degrees:\s*([^;\n]{1,160})", description, re.I)
    if listed:
        degree_hint += "; " + listed.group(1)
    if (not include_all and re.search(r"Ph\.?D|doctoral|master['’]?s|masters", degree_hint, re.I)
            and not re.search(r"bachelor|undergrad", degree_hint, re.I)):
        return None
    level = infer_level(title, description, raw.get("level_hint"), all_levels=include_all)
    if not level or level not in allowed_levels:
        return None
    notes = ["Potential match: confirm the employer's full requirements and work authorization."]
    if level == "co_op" or re.search(r"enrolled.*co.?op|co.?op program", description, re.I):
        notes.append("Check co-op program registration and student-status requirements.")
    if re.search(r"canadian citizen|citizenship|permanent residen", description, re.I):
        notes.append("Citizenship/permanent-residency restriction mentioned; eligibility is not assumed.")
    if re.search(r"security clearance|controlled goods|ITAR", description, re.I):
        notes.append("Security/export-control screening mentioned; check eligibility.")
    if re.search(r"bilingual|fluent.{0,25}french|fran[cç]ais", description, re.I):
        notes.append("Check French/bilingual language requirements.")
    if level == "new_grad":
        notes.append("Confirm the graduation window and start date; your graduation date is not assumed.")
    if re.search(r"master|Ph\.?D", description, re.I) or re.search(rf"\b{_ADVANCED_ABBREVIATION}\b", _credential_text(description)):
        notes.append("Check whether an advanced degree is required or only preferred.")
    # Neutral base score: tags describe the role, and personal ranking comes from the user's profile
    # (ranking.Matcher) at request time, so no interest is hard-coded here.
    if regions.is_canada(region):
        reason = "Canadian early-career opportunity" if level in EARLY_LEVELS else "Canadian opportunity"
    elif regions.is_global(region):
        reason = "Global opportunity"
    else:
        reason = f"{regions.label(region)} opportunity"
    score, tags, reasons = 40, [], [reason]
    if SPACE.search(company + " " + title):
        tags.append("aerospace")
    if EMBEDDED.search(title):
        tags.append("embedded")
    if re.search(r"Ontario|,\s*ON\b|\b(?:Toronto|Ottawa|Waterloo|Kitchener|Mississauga|Markham|Hamilton|London)\b", location, re.I):
        tags.append("ontario")
    if re.search(r"remote", location, re.I):
        tags.append("remote")
    if level == "new_grad":
        tags.append("new-grad")
    deadline, kind, evidence, deadline_source, uncertainty = _deadline_details(raw)
    if uncertainty:
        notes.append(_deadline_caveat(uncertainty))
    closed = bool(raw.get("is_closed"))
    if deadline and parsed_date(deadline) < now:
        closed = True
    posted = parsed_date(raw.get("posted_at"))
    if posted and posted < now - timedelta(days=180):
        closed = True
        notes.append("Posting is over 180 days old; archived until refreshed by the source.")
    verification = raw.get("verification") if raw.get("verification") in {"direct", "curated", "public_listing"} else "public_listing"
    if verification != "direct":
        notes.append("Discovered via a public/curated board; verify it is still open on the employer's site.")
    return dict(title=title[:400], company=company[:250], location=location[:1000], url=url,
                url_identity_policy=IDENTITY_POLICY, source_identity_policy=IDENTITY_POLICY,
                description=description, level=level, category=category_for(title), tags=tags,
                priority_score=min(score, 100), priority_reasons=reasons, eligibility_notes=notes,
                posted_at=iso(posted) if posted else None, deadline=deadline, deadline_kind=kind,
                deadline_date=parsed_date(deadline).astimezone(TORONTO).date().isoformat() if deadline and kind == "date" else None,
                deadline_evidence=evidence, deadline_source_url=deadline_source, deadline_uncertainty=uncertainty,
                first_seen=iso(now), last_seen=iso(now), active=not closed, status="new", verification=verification,
                salary=str(raw.get("salary"))[:300] if raw.get("salary") else None,
                source_id=str(raw.get("source_id") or "unknown"),
                source_job_id=str(raw.get("source_job_id") or url),
                sources=[dict(name=str(raw.get("source") or raw.get("source_id") or "Unknown source"),
                              url=url, type=str(raw.get("source_type") or "public_listing"))])


def dedup_key(job):
    company = re.sub(r"\b(?:incorporated|inc|llc|ltd|limited|corporation|corp)\b", "", fold(job["company"]))
    company = re.sub(r"[^a-z0-9]", "", company)
    title = re.sub(r"[^a-z0-9]", "", fold(job["title"]))
    location = fold(job["location"])
    cities = sorted({fold(m.group()) for m in CITIES.finditer(job["location"])})
    if cities:
        place = "|".join(cities)
    elif "remote" in location:
        # Keep remote postings with different country restrictions distinct.
        place = "remote-" + (re.sub(r"[^a-z0-9]", "", re.sub(r"\bremote\b", "", location)) or "anywhere")
    else:
        place = re.sub(r"[^a-z0-9]", "", location)
    return hashlib.sha256(f"{company}|{title}|{place}".encode()).hexdigest()


@contextmanager
def connection(db=DEFAULT_DB):
    if db is None:
        from . import config
        db = config.db_path()
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS aliases(source_id TEXT, source_job_id TEXT, job_id TEXT NOT NULL, url TEXT NOT NULL,
          active INTEGER NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(source_id, source_job_id));
      CREATE INDEX IF NOT EXISTS aliases_url ON aliases(url);
      CREATE INDEX IF NOT EXISTS aliases_job ON aliases(job_id);
      CREATE TABLE IF NOT EXISTS observations(source_id TEXT, source_job_id TEXT, job_id TEXT NOT NULL,
          data TEXT NOT NULL, PRIMARY KEY(source_id,source_job_id));
      CREATE INDEX IF NOT EXISTS observations_job ON observations(job_id);
      CREATE TABLE IF NOT EXISTS source_rejections(source_id TEXT, source_job_id TEXT, url TEXT NOT NULL,
          authority INTEGER NOT NULL, last_seen TEXT NOT NULL, url_identity_policy TEXT,
          PRIMARY KEY(source_id,source_job_id));
      CREATE INDEX IF NOT EXISTS rejection_url ON source_rejections(url);
      CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS reminders(job_id TEXT, deadline TEXT, band TEXT, emitted_at TEXT,
          PRIMARY KEY(job_id,deadline,band));
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db=DEFAULT_DB):
    with connection(db):
        pass


AUTHORITY = {"direct": 3, "public_listing": 2, "curated": 1}


def _rebuild_job(conn, job_id, now):
    """Choose one coherent authoritative observation, not the last finisher."""
    old_row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not old_row:
        return
    old = json.loads(old_row[0])
    records = conn.execute("""SELECT o.data,a.active,a.last_seen FROM observations o JOIN aliases a
        ON o.source_id=a.source_id AND o.source_job_id=a.source_job_id WHERE o.job_id=?""", (job_id,)).fetchall()
    if not records:
        return
    observed = [(json.loads(r["data"]), bool(r["active"]), r["last_seen"]) for r in records]
    observed.sort(key=lambda r: (-AUTHORITY.get(r[0]["verification"], 0), r[0]["source_id"], r[0]["source_job_id"]))
    chosen = dict(observed[0][0])
    rank = AUTHORITY.get(chosen["verification"], 0)
    authoritative = [r for r in observed if AUTHORITY.get(r[0]["verification"], 0) == rank]
    # A lower-authority copy may supply an otherwise unknown deadline, but it
    # cannot replace a published employer deadline or the employer's caveats.
    for candidate, _, _ in observed:
        if not chosen.get("deadline") and candidate.get("deadline"):
            for key in ("deadline", "deadline_kind", "deadline_date", "deadline_evidence", "deadline_source_url", "deadline_uncertainty"):
                chosen[key] = candidate.get(key)
            chosen["deadline_verification"] = candidate["verification"]
            if chosen.get("deadline_uncertainty"):
                caveat = _deadline_caveat(chosen["deadline_uncertainty"])
                if caveat not in chosen["eligibility_notes"]:
                    chosen["eligibility_notes"] = [*chosen["eligibility_notes"], caveat]
            break
    links = {(s["name"], s["url"]): s for data, _, _ in observed for s in data["sources"]}
    chosen.update(id=job_id, match_key=dedup_key(chosen), first_seen=old["first_seen"], status=old["status"],
                  identity_policy=old.get("identity_policy"),
                  last_seen=max(r[2] for r in authoritative), active=any(r[1] for r in authoritative),
                  sources=[links[k] for k in sorted(links)])
    if chosen.get("deadline") and parsed_date(chosen["deadline"]) <= now:
        chosen["active"] = False
    negatives = conn.execute("""SELECT DISTINCT r.* FROM source_rejections r JOIN aliases a
        ON r.url=a.url WHERE a.job_id=? AND r.authority>=?""", (job_id, rank)).fetchall()
    aliases = conn.execute("""SELECT a.*,o.data AS observation FROM aliases a LEFT JOIN observations o
        ON o.source_id=a.source_id AND o.source_job_id=a.source_job_id WHERE a.job_id=?""", (job_id,)).fetchall()
    ambiguous = False
    for negative in negatives:
        same_source = [a for a in aliases if a["source_id"] == negative["source_id"]]
        # A known different requisition is not evidence about this job.
        if same_source and not any(a["source_job_id"] == negative["source_job_id"] for a in same_source):
            continue
        if not same_source:
            # An unbound rejection is evidence only through its URL. Once that
            # URL is shared by distinct requisitions it identifies none of them.
            if _shared_url(conn, negative["url"]):
                ambiguous = True
                continue
            if not any(_proven_alias_url(a) == negative["url"] for a in aliases):
                raise ValueError("Legacy rejection/alias URL equality requires manual identity review")
        chosen["active"] = False
        chosen["eligibility_notes"] = list(chosen["eligibility_notes"]) + ["An authoritative source does not meet the current matching rules; this copy is archived."]
        break
    else:
        if ambiguous:
            chosen["eligibility_notes"] = list(chosen["eligibility_notes"]) + [SHARED_LINK_NOTE]
    conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(chosen, ensure_ascii=False), job_id))


SHARED_LINK_NOTE = ("Another source listed a posting at this link that does not meet the matching rules, but several "
                    "requisitions share the link; confirm this role's requirements on the employer's site.")


def _shared_url(conn, url, incoming=None):
    """Accepted aliases and the rejection ledger share one ambiguity rule: a URL
    stops identifying one posting once any source shows two requisitions there."""
    keys = {}
    for row in conn.execute("""SELECT source_id,source_job_id FROM aliases WHERE url=?
            UNION SELECT source_id,source_job_id FROM source_rejections WHERE url=?""", (url, url)):
        keys.setdefault(row[0], set()).add(row[1])
    if incoming:
        keys.setdefault(incoming[0], set()).add(incoming[1])
    return any(len(found) > 1 for found in keys.values())


def _jobs_at(conn, urls):
    """Every job whose negative evidence or URL ambiguity can change with these URLs."""
    found = set()
    for url in {u for u in urls if u}:
        found.update(row[0] for row in conn.execute("SELECT DISTINCT job_id FROM aliases WHERE url=?", (url,)))
    return found


def _proven_alias_url(alias):
    """Old normalized URLs may have lost requisition parameters irreversibly."""
    observation = json.loads(alias["observation"]) if alias["observation"] else {}
    if observation.get("url_identity_policy") == IDENTITY_POLICY and observation.get("url") == alias["url"]:
        return alias["url"]
    return None


def _require_identity_evidence(conn):
    """Check row evidence even when an earlier migration advanced global markers."""
    aliases = conn.execute("""SELECT a.*,o.data AS observation FROM aliases a LEFT JOIN observations o
        ON o.source_id=a.source_id AND o.source_job_id=a.source_job_id
        WHERE a.source_job_id LIKE 'http:%' OR a.source_job_id LIKE 'https:%'""").fetchall()
    for alias in aliases:
        observation = json.loads(alias["observation"]) if alias["observation"] else {}
        # An old URL-shaped key may be a lossy fallback, not a native ID. Even
        # exact-v2 URL flags could have been written by the old alias-lookup bug;
        # only a newly observed, key-bound source proof can certify this path.
        if (observation.get("source_identity_policy") != IDENTITY_POLICY
            or any(observation.get(key) != alias[key] for key in ("source_id", "source_job_id"))
            or not _proven_alias_url(alias)):
            raise ValueError("Legacy URL-derived source identity requires manual identity review")
    # Add nullable row provenance inside the caller's write transaction. Never
    # backfill old negatives from their URLs or the database policy marker.
    if "url_identity_policy" not in {r["name"] for r in conn.execute("PRAGMA table_info(source_rejections)")}:
        conn.execute("ALTER TABLE source_rejections ADD COLUMN url_identity_policy TEXT")
    if conn.execute("SELECT 1 FROM source_rejections WHERE url_identity_policy IS NOT ? LIMIT 1",
                    (IDENTITY_POLICY,)).fetchone():
        raise ValueError("Legacy source rejection provenance requires manual identity review")


def _migrate_legacy_identities(conn, now):
    """Split unproven legacy merges; never promote discarded URL information."""
    _require_identity_evidence(conn)
    marker = conn.execute("SELECT value FROM meta WHERE key='identity_policy_version'").fetchone()
    if marker:
        version = json.loads(marker[0])
        if type(version) is not int or not 1 <= version <= IDENTITY_POLICY_VERSION:
            raise ValueError("Unsupported legacy identity policy requires manual identity review")
        if version == IDENTITY_POLICY_VERSION:
            return
    conflicts = {r[0] for r in conn.execute("""SELECT url FROM (SELECT url,source_id,source_job_id FROM aliases
        UNION SELECT url,source_id,source_job_id FROM source_rejections)
        GROUP BY url,source_id HAVING COUNT(DISTINCT source_job_id)>1""")}
    legacy = conn.execute("SELECT * FROM jobs").fetchall()
    for stored in legacy:
        old = json.loads(stored["data"])
        if old.get("identity_policy") == IDENTITY_POLICY:
            continue
        aliases = conn.execute("""SELECT a.*,o.data AS observation FROM aliases a LEFT JOIN observations o
            ON o.source_id=a.source_id AND o.source_job_id=a.source_job_id WHERE a.job_id=?
            ORDER BY a.source_id,a.source_job_id""", (stored["id"],)).fetchall()
        groups = {}
        for alias in aliases:
            proven = _proven_alias_url(alias)
            key = ("url", proven) if proven and proven not in conflicts else ("source", alias["source_id"], alias["source_job_id"])
            groups.setdefault(key, []).append(alias)
        if len(groups) > 1:
            notification_state = conn.execute("""SELECT value FROM meta
                WHERE key IN ('pending_digest','pending_reminders','digest_announced_ids')""").fetchall()
            if (old["status"] != "new"
                or any(stored["id"] in row[0] for row in notification_state)
                or conn.execute("SELECT 1 FROM reminders WHERE job_id=? LIMIT 1", (stored["id"],)).fetchone()):
                raise ValueError("Legacy merged application/notification state requires manual identity review")
            if any(a["observation"] is None for a in aliases):
                raise ValueError("Legacy merged job lacks source observations needed for a safe split")
            groups = sorted(groups.values(), key=lambda group: (
                not any(a["source_id"] == old["source_id"] and a["source_job_id"] == old["source_job_id"] for a in group),
                not any(a["url"] == old["url"] for a in group), group[0]["source_id"], group[0]["source_job_id"]))
            rebuilt = [stored["id"]]
            for group in groups[1:]:
                representative = group[0]
                key = hashlib.sha256(f"{IDENTITY_POLICY}|{stored['id']}|{representative['source_id']}|{representative['source_job_id']}|{representative['url']}".encode()).hexdigest()
                job_id = key[:24]
                initial = json.loads(representative["observation"])
                initial.update(id=job_id, match_key=dedup_key(initial), status="new", identity_policy=IDENTITY_POLICY)
                conn.execute("INSERT INTO jobs(id,dedup_key,data) VALUES(?,?,?)", (job_id, key, json.dumps(initial)))
                for alias in group:
                    identity = (job_id, alias["source_id"], alias["source_job_id"])
                    conn.execute("UPDATE aliases SET job_id=? WHERE source_id=? AND source_job_id=?", identity)
                    conn.execute("UPDATE observations SET job_id=? WHERE source_id=? AND source_job_id=?", identity)
                rebuilt.append(job_id)
            old["identity_policy"] = IDENTITY_POLICY
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(old), stored["id"]))
            for job_id in rebuilt:
                _rebuild_job(conn, job_id, now)
        else:
            old["identity_policy"] = IDENTITY_POLICY
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(old), stored["id"]))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('identity_policy_version',?)", (json.dumps(IDENTITY_POLICY_VERSION),))


def _cross_source_candidate(conn, normalized):
    """An identical canonical posting URL is evidence; a title fingerprint is not.
    A URL already shared by distinct requisitions, accepted or rejected, lends
    no identity or application state to another source's copy."""
    if _shared_url(conn, normalized["url"], (normalized["source_id"], normalized["source_job_id"])):
        return None
    candidates = conn.execute("""SELECT DISTINCT j.* FROM jobs j JOIN aliases a ON j.id=a.job_id
        WHERE a.url=?""", (normalized["url"],)).fetchall()
    eligible = []
    for row in candidates:
        aliases = conn.execute("""SELECT a.*,o.data AS observation FROM aliases a LEFT JOIN observations o
            ON o.source_id=a.source_id AND o.source_job_id=a.source_job_id WHERE a.job_id=?""", (row["id"],)).fetchall()
        if any(a["source_id"] == normalized["source_id"] and a["source_job_id"] != normalized["source_job_id"] for a in aliases):
            continue
        if not any(_proven_alias_url(alias) == normalized["url"] for alias in aliases):
            continue
        eligible.append(row)
    return eligible[0] if len(eligible) == 1 else None


def upsert_jobs(db, rows, now=None, *, region=None, levels=None):
    now = now or utcnow()
    result = {"raw": 0, "matched": 0, "new": 0, "updated": 0, "rejected": 0, "new_ids": []}
    with connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _migrate_legacy_identities(conn, now)
        for raw in rows:
            result["raw"] += 1
            sid, jid = str(raw.get("source_id") or "unknown"), str(raw.get("source_job_id") or canonical_url(raw.get("url")) or "")
            existing = conn.execute("SELECT j.* FROM jobs j JOIN aliases a ON j.id=a.job_id WHERE a.source_id=? AND a.source_job_id=?", (sid, jid)).fetchone()
            normalized = normalize_job(raw, now, region=region, levels=levels)
            # This row can move its own ledger entry and alias between URLs.
            # Every job at an old or new URL is re-evaluated, because the
            # ambiguity of a URL decides which evidence applies to those jobs.
            touched = {row[0] for row in conn.execute("""SELECT url FROM source_rejections WHERE source_id=? AND source_job_id=?
                UNION SELECT url FROM aliases WHERE source_id=? AND source_job_id=?""", (sid, jid, sid, jid))}
            rebuild = set()
            if not normalized:
                result["rejected"] += 1
                rejected_url = canonical_url(raw.get("url"))
                if rejected_url and raw.get("title") and raw.get("company"):
                    authority = AUTHORITY.get(raw.get("verification"), AUTHORITY["public_listing"])
                    conn.execute("""INSERT OR REPLACE INTO source_rejections
                        (source_id,source_job_id,url,authority,last_seen,url_identity_policy) VALUES(?,?,?,?,?,?)""",
                        (sid, jid, rejected_url, authority, iso(now), IDENTITY_POLICY))
                if existing:
                    previous = conn.execute("SELECT data FROM observations WHERE source_id=? AND source_job_id=?", (sid, jid)).fetchone()
                    historical = json.loads(previous[0] if previous else existing["data"])
                    historical.update(source_id=sid, source_job_id=jid, active=False,
                                      verification=raw.get("verification") if raw.get("verification") in AUTHORITY else "public_listing")
                    historical["eligibility_notes"] = historical.get("eligibility_notes", []) + ["This source no longer meets the matching rules."]
                    historical["sources"] = [{"name": str(raw.get("source") or sid), "url": canonical_url(raw.get("url")) or historical["url"], "type": str(raw.get("source_type") or "public_listing")}]
                    conn.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?)", (sid, jid, existing["id"], json.dumps(historical)))
                    conn.execute("UPDATE aliases SET active=0,last_seen=? WHERE source_id=? AND source_job_id=?", (iso(now), sid, jid))
                    rebuild.add(existing["id"])
                for job_id in sorted(rebuild | _jobs_at(conn, touched | {rejected_url})):
                    _rebuild_job(conn, job_id, now)
                continue
            conn.execute("DELETE FROM source_rejections WHERE source_id=? AND source_job_id=?", (sid, jid))
            result["matched"] += 1
            if existing is None:
                existing = _cross_source_candidate(conn, normalized)
            if existing:
                job_id = existing["id"]
                result["updated"] += 1
            else:
                key = hashlib.sha256(f"{sid}|{jid}|{normalized['url']}".encode()).hexdigest()
                job_id = key[:24]
                initial = normalized | {"id": job_id, "match_key": dedup_key(normalized), "identity_policy": IDENTITY_POLICY}
                conn.execute("INSERT INTO jobs(id,dedup_key,data) VALUES(?,?,?)", (job_id, key, json.dumps(initial)))
                result["new"] += 1
                result["new_ids"].append(job_id)
            conn.execute("INSERT OR REPLACE INTO aliases VALUES(?,?,?,?,?,?)", (sid, jid, job_id, normalized["url"], int(normalized["active"]), iso(now)))
            conn.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?)", (sid, jid, job_id, json.dumps(normalized, ensure_ascii=False)))
            for affected in sorted({job_id} | _jobs_at(conn, touched | {normalized["url"]})):
                _rebuild_job(conn, affected, now)
    return result


def reconcile_source(db, source_id, seen_ids, now=None):
    """Close absent aliases only after a confirmed complete source snapshot."""
    now = now or utcnow()
    seen = {str(i) for i in seen_ids}
    changed = set()
    with connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _migrate_legacy_identities(conn, now)
        rows = conn.execute("SELECT * FROM aliases WHERE source_id=? AND active=1", (source_id,)).fetchall()
        for row in rows:
            if row["source_job_id"] not in seen:
                conn.execute("UPDATE aliases SET active=0 WHERE source_id=? AND source_job_id=?", (source_id, row["source_job_id"]))
                changed.add(row["job_id"])
        for job_id in changed:
            _rebuild_job(conn, job_id, now)
    return len(changed)


def all_jobs(db=DEFAULT_DB):
    with connection(db) as conn:
        return [json.loads(r["data"]) for r in conn.execute("SELECT data FROM jobs")]


def get_job(db, job_id):
    with connection(db) as conn:
        row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row["data"]) if row else None


def set_status(db, job_id, status):
    if status not in STATES:
        raise ValueError("Invalid application status")
    with connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        job = json.loads(row["data"]); job["status"] = status
        conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job_id))
        return job


def list_jobs(db=DEFAULT_DB, q="", level="", category="", location="", status="", sort="priority", deadline_days=None,
              page=1, page_size=30, priority_only=False, include_closed=False, now=None, unpaged=False,
              region=None, levels=None):
    now = now or utcnow()
    wanted_levels = set(levels or ())
    rows = []
    for j in all_jobs(db):
        if not include_closed and (not j["active"] or (j.get("deadline") and parsed_date(j["deadline"]) < now)):
            continue
        if not status and j["status"] == "dismissed":
            continue
        if status and status not in {"all", "any"} and j["status"] != status:
            continue
        if wanted_levels and j.get("level") not in wanted_levels:
            continue
        if region is not None and not regions.matches(j.get("location"), region, j.get("country_hint")):
            continue
        if q and fold(q) not in fold(" ".join(str(j.get(x) or "") for x in ("title", "company", "location", "description"))):
            continue
        if level and j["level"] != level:
            continue
        if category and j["category"] != category:
            continue
        if location and fold(location) not in fold(j["location"]):
            continue
        if priority_only and not j.get("priority_match"):  # set by the web app's profile matcher
            continue
        if deadline_days is not None:
            deadline = parsed_date(j.get("deadline"))
            if not deadline or not now <= deadline <= now + timedelta(days=int(deadline_days)):
                continue
        rows.append(j)
    if sort == "newest":
        rows.sort(key=lambda j: (j.get("posted_at") or j["first_seen"], j["priority_score"]), reverse=True)
    elif sort == "deadline":
        rows.sort(key=lambda j: (j.get("deadline") or "9999", -j["priority_score"]))
    else:
        rows.sort(key=lambda j: (j["priority_score"], j.get("posted_at") or j["first_seen"]), reverse=True)
    if unpaged:  # callers that re-rank (profile matching) page the result themselves
        return {"items": rows, "total": len(rows), "page": 1, "page_size": len(rows), "pages": 1}
    page, page_size = max(1, int(page)), min(250, max(1, int(page_size)))
    return {"items": rows[(page - 1) * page_size:page * page_size], "total": len(rows), "page": page,
            "page_size": page_size, "pages": math.ceil(len(rows) / page_size)}


def retire_sources(db, keep_ids):
    """Drop health records of sources no longer in the registry, so board counts and source
    problems reflect only monitored boards. Their jobs stay and expire normally when unseen."""
    keep = set(keep_ids)
    with connection(db) as conn:
        gone = sorted(row[0] for row in conn.execute("SELECT id FROM sources") if row[0] not in keep)
        conn.executemany("DELETE FROM sources WHERE id=?", [(ident,) for ident in gone])
    return gone


def record_source(db, source, *, status, raw_count=0, matched_count=0, error=None, now=None):
    now = now or utcnow()
    data = {k: source.get(k) for k in ("id", "name", "type", "url", "coverage", "notes")}
    data["coverage"] = source.get("coverage") or source.get("coverage_note")
    data["notes"] = source.get("notes") or source.get("disabled_reason") or source.get("maintenance_note")
    data.update(status=status, last_checked=iso(now), raw_count=raw_count, matched_count=matched_count, error=str(error)[:1200] if error else None)
    with connection(db) as conn:
        conn.execute("INSERT INTO sources(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (source["id"], json.dumps(data)))


def sources(db=DEFAULT_DB):
    with connection(db) as conn:
        return [json.loads(row["data"]) for row in conn.execute("SELECT data FROM sources ORDER BY id")]


def set_meta(db, key, value):
    with connection(db) as conn:
        conn.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def get_meta(db, key, default=None):
    with connection(db) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def expire_stale(db=DEFAULT_DB, now=None, days=14):
    now = now or utcnow()
    count = 0
    with connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in conn.execute("SELECT id,data FROM jobs").fetchall():
            j = json.loads(row["data"])
            deadline = parsed_date(j.get("deadline"))
            if j["active"] and ((deadline and deadline < now) or parsed_date(j["last_seen"]) < now - timedelta(days=days)):
                j["active"] = False
                conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(j), row["id"]))
                count += 1
    return count


def due_reminders(db=DEFAULT_DB, now=None):
    now = now or utcnow()
    items = []
    with connection(db) as conn:
        sent = {(r["job_id"], r["deadline"], r["band"]) for r in conn.execute("SELECT * FROM reminders")}
        jobs = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM jobs")]
    for j in jobs:
        if not j["active"] or j["status"] in {"applied", "dismissed"} or not j.get("deadline"):
            continue
        remaining = (parsed_date(j["deadline"]) - now).total_seconds()
        if remaining <= 0:
            continue
        if j.get("deadline_kind") == "date":
            days = (parsed_date(j["deadline"]).astimezone(TORONTO).date() - now.astimezone(TORONTO).date()).days
            if days > 7:
                continue
            band = "1d" if days <= 1 else "3d" if days <= 3 else "7d"
        else:
            if remaining > 7 * 86400:
                continue
            band = "1d" if remaining <= 86400 else "3d" if remaining <= 3 * 86400 else "7d"
        if (j["id"], j["deadline"], band) not in sent:
            items.append(j | {"reminder_band": band})
    return sorted(items, key=lambda j: (j["deadline"], -j["priority_score"]))


def mark_reminders(db, jobs, now=None):
    with connection(db) as conn:
        conn.executemany("INSERT OR IGNORE INTO reminders VALUES(?,?,?,?)", [(j["id"], j["deadline"], j["reminder_band"], iso(now or utcnow())) for j in jobs])


def stats(db=DEFAULT_DB, now=None):
    now = now or utcnow()
    jobs = [j for j in all_jobs(db) if j["active"] and j["status"] != "dismissed" and (not j.get("deadline") or parsed_date(j["deadline"]) > now)]
    ss = sources(db)
    today = now.astimezone(TORONTO).date()
    def counts(key):
        return [{"name": k, "count": v} for k, v in sorted(Counter(j[key] for j in jobs).items())]
    running = get_meta(db, "collection_running", False)
    started = parsed_date(get_meta(db, "collection_started"))
    # next_refresh/timezone are filled in by the web app from the user's schedule.
    return dict(total=len(jobs), new_today=sum(parsed_date(j["first_seen"]).astimezone(TORONTO).date() == today for j in jobs),
                priority=0,
                closing_soon=sum(bool(j.get("deadline") and now < parsed_date(j["deadline"]) <= now + timedelta(days=7)) for j in jobs),
                saved=sum(j["status"] == "saved" for j in jobs), applied=sum(j["status"] == "applied" for j in jobs),
                sources_ok=sum(s["status"] in {"ok", "partial"} for s in ss), sources_total=len(ss),
                last_refresh=get_meta(db, "last_refresh"), next_refresh=None,
                timezone=str(TORONTO), categories=counts("category"), levels=counts("level"), location_counts=counts("location"),
                collection_running=bool(running and started and now - started < timedelta(minutes=MAX_COLLECTION_MINUTES + 10)))
