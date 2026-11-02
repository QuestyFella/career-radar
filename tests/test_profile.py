"""Offline tests for career_radar.profile: storage, PDF reading, heuristics, LLM extraction, merging.

All resume data here is synthetic and fictional ("Alex Example", example.com). PDFs are built by hand
(catalog, pages, Helvetica font, content streams, exact xref offsets), so no files or network are needed.
"""
from __future__ import annotations

import io
import json
import os
import stat

import pytest

from career_radar import profile as P

# --------------------------------------------------------------------------- hand-built PDFs


def pdf_string(text: str) -> bytes:
    """A PDF literal string in WinAnsi (cp1252) encoding with (, ) and \\ escaped."""
    out = bytearray(b"(")
    for byte in text.encode("cp1252"):
        if byte in b"()\\":
            out += b"\\" + bytes([byte])
        elif byte < 32 or byte > 126:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    return bytes(out + b")")


def make_pdf(pages, info=None) -> bytes:
    """A minimal valid text PDF: one content stream per page of lines, optional /Info metadata."""
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"}
    kids, next_id = [], 4
    for lines in pages:
        ops = [b"BT", b"/F1 11 Tf", b"14 TL", b"72 740 Td"]
        ops += [pdf_string(line) + b" Tj T*" for line in lines]
        ops.append(b"ET")
        stream = b"\n".join(ops)
        content_id, page_id = next_id, next_id + 1
        next_id += 2
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objects[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_id)
        kids.append(page_id)
    objects[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    info_id = None
    if info:
        info_id = next_id
        objects[info_id] = b"<< " + b" ".join(b"/" + k.encode() + b" " + pdf_string(v) for k, v in info.items()) + b" >>"
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    trailer = b"<< /Size %d /Root 1 0 R" % size + (b" /Info %d 0 R" % info_id if info_id else b"") + b" >>"
    out += b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


RESUME_LINES = [
    "ALEX EXAMPLE",
    "Embedded Software Student",
    "Toronto, ON | alex@example.com | (416) 555-0199 | github.com/alex-example",
    "linkedin.com/in/alex-example",
    "EDUCATION",
    "University of Exampleton - Bachelor of Computer Science (Honours)",
    "Sep 2023 – Apr 2027",
    "EXPERIENCE",
    "Firmware Developer Intern, Northwind Avionics | May 2025 – Aug 2025",
    "- Wrote C++ drivers for an STM32 flight controller using FreeRTOS",
    "Teaching Assistant, University of Exampleton | Sep 2024 – Present",
    "- Ran Python and Linux labs for 60 students",
    "PROJECTS",
    "CubeSat Telemetry Board: firmware for a student satellite team; CAN bus and SPI sensors",
    "Ground station dashboard in React and TypeScript for satellite passes",
    "SKILLS",
    "Languages: Python, C, C++, SQL, Bash",
    "Tools: Git, Docker, Linux, KiCad",
]
RESUME_INFO = {"Title": "Alex Example Resume", "Author": "Alex Example", "Creator": "Example Writer",
               "Producer": "Hand-built test PDF", "CreationDate": "D:20261015120000Z"}


@pytest.fixture(scope="module")
def resume_pdf() -> bytes:
    return make_pdf([RESUME_LINES], RESUME_INFO)


@pytest.fixture(scope="module")
def resume_text(resume_pdf) -> str:
    return P.read_pdf(resume_pdf)["text"]


class FakeClient:
    """Stands in for llm.Client: records calls and returns (or raises) a canned reply."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def complete_json(self, system, user, *, max_tokens=2500):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        return self.reply


LLM_REPLY = {
    "name": "Alex Example", "email": "alex@example.com", "location": "Toronto, ON",
    "headline": "Embedded software student", "graduation": "Apr 2027",
    "education": [{"school": "University of Exampleton", "degree": "Bachelor of Computer Science (Honours)",
                   "field": "Computer Science", "graduation": "Apr 2027"}],
    "experience": [{"title": "Firmware Developer Intern", "company": "Northwind Avionics", "start": "May 2025",
                    "end": "Aug 2025"}],
    "skills": ["Python", "C++", "FreeRTOS", "STM32"],
    "interests": ["Aerospace", "Embedded", "space"],
    "links": ["https://github.com/alex-example"],
}


# --------------------------------------------------------------------------- normalize / storage

def test_empty_profile_has_every_key_and_all_levels():
    empty = P.empty_profile()
    assert tuple(empty) == P.PROFILE_KEYS
    assert empty["levels"] == list(P.LEVELS) and empty["source"] == "manual"
    assert P.normalize_profile(None) == empty and P.normalize_profile("junk") == empty


def test_normalize_profile_cleans_types_caps_and_drops_unknown_keys():
    raw = {
        "name": "  Alex   Example \n", "email": 42, "location": ["not", "a", "string"], "headline": "h" * 400,
        "graduation": "x" * 500, "password": "nope", "skills": ["Python", "python", " PYTHON ", "", None, 3, "C++"]
        + [f"skill{i}" for i in range(100)], "interests": "embedded, aerospace;embedded", "links":
        ["https://github.com/alex-example", "github.com/alex-example/", "javascript:alert(1)", "ftp://x.example",
         "linkedin.com/in/alex-example", "Node.js"] + [f"https://site{i}.example.com" for i in range(20)],
        "education": [{"school": "University of Exampleton", "degree": "BSc", "extra": "drop", "field": 7}, "bad",
                      {}] + [{"school": f"School {i}"} for i in range(20)],
        "experience": {"title": "not a list"}, "levels": ["co-op", "bogus", "NEW_GRAD"], "source": "hacker",
        "exclude_keywords": [f"w{i}" for i in range(60)], "preferred_locations": [f"City {i}" for i in range(30)],
        "updated_at": "2026-10-01T00:00:00+00:00",
    }
    p = P.normalize_profile(raw)
    assert list(p) == list(P.PROFILE_KEYS)
    assert "password" not in p
    assert p["name"] == "Alex Example" and p["email"] == "42" and p["location"] == ""
    assert len(p["headline"]) == 300 and len(p["graduation"]) == 200
    assert p["skills"][:2] == ["Python", "C++"] and len(p["skills"]) == 80
    assert p["interests"] == ["embedded", "aerospace"]
    assert p["links"][:2] == ["https://github.com/alex-example", "https://linkedin.com/in/alex-example"]
    assert len(p["links"]) == 10 and all(link.startswith("https://") for link in p["links"])
    assert p["education"][0] == {"school": "University of Exampleton", "degree": "BSc", "field": "7", "graduation": ""}
    assert len(p["education"]) == 10 and all(set(e) == set(P.EDUCATION_KEYS) for e in p["education"])
    assert p["experience"] == []
    assert p["levels"] == ["co_op", "new_grad"]
    assert p["source"] == "manual" and p["updated_at"] == "2026-10-01T00:00:00+00:00"
    assert len(p["exclude_keywords"]) == 40 and len(p["preferred_locations"]) == 20
    assert P.normalize_profile({"levels": ["bogus"]})["levels"] == list(P.LEVELS)
    assert P.normalize_profile({"interests": [f"t{i}" for i in range(40)]})["interests"][-1] == "t24"
    assert P.normalize_profile({"source": "resume+llm"})["source"] == "resume+llm"


def test_save_load_delete_profile_round_trip(tmp_path):
    assert P.load_profile(tmp_path) == P.empty_profile()
    saved = P.save_profile(tmp_path, {"name": "Alex Example", "skills": ["Python"], "bogus": 1})
    assert saved["updated_at"].endswith("+00:00") and "bogus" not in saved
    on_disk = json.loads((tmp_path / "profile.json").read_text())
    assert on_disk == saved == P.load_profile(tmp_path)
    assert stat.S_IMODE(os.stat(tmp_path / "profile.json").st_mode) == 0o600
    P.save_resume(tmp_path, {"filename": "cv.pdf", "pages": 1, "chars": 10, "method": "heuristic"}, "secret text")
    P.delete_profile(tmp_path)
    assert not (tmp_path / "profile.json").exists() and not (tmp_path / "resume.json").exists()
    assert P.load_profile(tmp_path) == P.empty_profile()
    P.delete_profile(tmp_path)   # idempotent


def test_resume_info_never_exposes_text(tmp_path):
    assert P.load_resume(tmp_path) is None
    P.save_resume(tmp_path, {"filename": "cv.pdf", "pages": 2, "chars": 11, "method": "llm", "pdf": {"title": "T"},
                             "warnings": ["w"], "text": "should not leak", "uploaded_at": "2026-11-01T00:00:00+00:00"},
                  "full resume text")
    info = P.load_resume(tmp_path)
    assert "text" not in info and info["filename"] == "cv.pdf" and info["method"] == "llm"
    assert info["pdf"] == {"title": "T", "author": "", "creator": "", "producer": "", "created": ""}
    assert json.loads((tmp_path / "resume.json").read_text())["text"] == "full resume text"
    P.delete_resume(tmp_path)
    assert P.load_resume(tmp_path) is None
    (tmp_path / "resume.json").write_text("{not json")
    assert P.load_resume(tmp_path) is None


# --------------------------------------------------------------------------- read_pdf

def test_read_pdf_text_pages_and_metadata(resume_pdf):
    result = P.read_pdf(resume_pdf)
    assert set(result) == {"text", "pages", "pdf", "warnings"}
    assert result["pages"] == 1 and result["warnings"] == []
    assert result["pdf"] == {"title": "Alex Example Resume", "author": "Alex Example", "creator": "Example Writer",
                             "producer": "Hand-built test PDF", "created": "2026-10-15"}
    text = result["text"]
    assert text.splitlines()[0] == "ALEX EXAMPLE"
    assert "Sep 2023 – Apr 2027" in text and "Bachelor of Computer Science (Honours)" in text
    assert len(text.splitlines()) == len(RESUME_LINES)


def test_read_pdf_without_metadata_and_multi_page_cap():
    data = make_pdf([[f"Page {n} line about embedded firmware and satellites " * 3] for n in range(1, 5)])
    result = P.read_pdf(data, max_pages=2)
    assert result["pages"] == 4
    assert "Page 1" in result["text"] and "Page 2" in result["text"] and "Page 3" not in result["text"]
    assert any("first 2 of 4 pages" in w for w in result["warnings"])
    assert result["pdf"] == {"title": "", "author": "", "creator": "", "producer": "", "created": ""}


def test_read_pdf_collapses_whitespace_but_keeps_lines():
    data = make_pdf([["Alex      Example", "", "Line\ttwo   here"]])
    text = P.read_pdf(data)["text"]
    assert "Alex Example" in text and "Line two here" in text and "\n" in text


def test_read_pdf_little_text_warning():
    result = P.read_pdf(make_pdf([["Scanned"]]))
    assert result["text"] == "Scanned"
    assert P.LITTLE_TEXT_WARNING in result["warnings"]
    assert P.LITTLE_TEXT_WARNING == ("Very little selectable text was found (scanned PDF?). "
                                     "Fill in your profile manually.")


@pytest.mark.parametrize("data", [b"", b"hello world", b"PK\x03\x04 zip file", b"   %PDF-1.4 leading space"])
def test_read_pdf_rejects_non_pdf(data):
    with pytest.raises(P.ResumeError, match="Not a PDF file"):
        P.read_pdf(data)


def test_read_pdf_rejects_oversize_before_parsing():
    with pytest.raises(P.ResumeError, match="larger than 8 MB"):
        P.read_pdf(b"%PDF-1.4\n" + b"0" * (8 * 1024 * 1024))


@pytest.mark.parametrize("data", [b"%PDF-1.4\ngarbage", b"%PDF-1.7\n" + b"x" * 500,
                                  b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"])
def test_read_pdf_broken_pdf_is_a_resume_error(data):
    with pytest.raises(P.ResumeError, match="Could not read this PDF"):
        P.read_pdf(data)


def test_read_pdf_encrypted():
    pypdf = pytest.importorskip("pypdf")

    def encrypted(user_password):
        writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(make_pdf([["Alex Example", "Encrypted"]]))))
        writer.encrypt(user_password=user_password, owner_password="owner-secret", algorithm="RC4-128")
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    with pytest.raises(P.ResumeError, match="password-protected"):
        P.read_pdf(encrypted("user-secret"))
    assert "Encrypted" in P.read_pdf(encrypted(""))["text"]   # empty user password: readable


# --------------------------------------------------------------------------- heuristics

def test_heuristic_extract_fields(resume_text):
    got = P.heuristic_extract(resume_text)
    assert set(got) == set(P.EXTRACT_KEYS) | {"levels"}
    assert got["name"] == "Alex Example"
    assert got["email"] == "alex@example.com"
    assert got["location"] == "Toronto, ON"
    assert got["headline"] == "Embedded Software Student"
    assert got["links"] == ["https://github.com/alex-example", "https://linkedin.com/in/alex-example"]
    assert got["education"] == [{"school": "University of Exampleton", "field": "Computer Science",
                                 "degree": "Bachelor of Computer Science (Honours)", "graduation": "Apr 2027"}]
    assert got["graduation"] == "Apr 2027"
    assert got["experience"] == [
        {"title": "Firmware Developer Intern", "company": "Northwind Avionics", "start": "May 2025", "end": "Aug 2025"},
        {"title": "Teaching Assistant", "company": "University of Exampleton", "start": "Sep 2024", "end": "Present"}]
    for skill in ("Python", "C", "C++", "SQL", "Bash", "Git", "Docker", "Linux", "KiCad", "STM32", "FreeRTOS",
                  "CAN bus", "SPI", "React", "TypeScript"):
        assert skill in got["skills"]
    assert len(got["skills"]) == len({s.casefold() for s in got["skills"]})
    assert got["interests"][:2] == ["aerospace", "embedded"]
    assert got["levels"] == list(P.LEVELS)


def test_heuristic_never_extracts_phone_or_street_address(resume_text):
    text = resume_text + "\n123 King Street West, Waterloo, ON N2L 3G1 | +1 (519) 555-0123"
    got = P.heuristic_extract(text)
    dumped = json.dumps(got)
    for fragment in ("555", "0199", "0123", "416", "King Street", "N2L"):
        assert fragment not in dumped
    only_street = P.heuristic_extract("Alex Example\n12 Elm Street, ON\nalex@example.com")
    assert only_street["location"] == ""


def test_heuristic_graduation_only_from_explicit_phrases():
    started_only = P.heuristic_extract("Alex Example\nEDUCATION\nUniversity of Exampleton\n"
                                       "Bachelor of Science in Computer Science\nStarted September 2023\n")
    assert started_only["graduation"] == "" and started_only["education"][0]["graduation"] == ""
    ongoing = P.heuristic_extract("EDUCATION\nUniversity of Exampleton, BSc Computer Science, Sep 2023 – Present\n")
    assert ongoing["graduation"] == ""
    expected = P.heuristic_extract("Alex Example\nEducation:\nExampleton College — Advanced Diploma, "
                                   "Computer Programming\nExpected Graduation: 2027\n")
    assert expected["graduation"] == "Expected 2027"
    assert expected["education"][0]["degree"] == "Advanced Diploma"
    assert expected["education"][0]["field"] == "Computer Programming"
    phrase = P.heuristic_extract("Education\nUniversité Exemple, BEng Software Engineering, Expected April 2027")
    assert phrase["graduation"] == "Expected April 2027"
    assert P.heuristic_extract("Class of 2026\nUniversity of Exampleton BSc Mathematics")["graduation"] == "Class of 2026"


def test_heuristic_spaced_headers_titles_and_inline_skills():
    text = """CURRICULUM VITAE
JORDAN SAMPLE-LEE
jordan.sample@example.org | Waterloo, Ontario | https://jordan.example.dev
E D U C A T I O N
Exampleton Polytechnic Institute
B.Eng. Software Engineering, Expected Graduation: April 2027
Work Experience
Example Robotics Inc. | Waterloo, ON
Software Developer Co-op, Jan 2025 - Apr 2025
- Built R&D tooling in Go and Python; met the C-suite about a Series C round; drew parts in AutoCAD
Technical Skills
Programming Languages: Go, R, C#, .NET, Node.js
Languages: English, French
"""
    got = P.heuristic_extract(text)
    assert got["name"] == "Jordan Sample-Lee"
    assert got["location"] == "Waterloo, Ontario"
    assert got["education"][0]["school"] == "Exampleton Polytechnic Institute"
    assert got["graduation"] == "Expected April 2027"
    assert got["experience"] == [{"title": "Software Developer Co-op", "company": "Example Robotics Inc.",
                                  "start": "Jan 2025", "end": "Apr 2025"}]
    assert got["skills"][:5] == ["Go", "R", "C#", ".NET", "Node.js"]
    assert "C" not in got["skills"]          # C-suite / Series C are not the C language
    assert "English" not in got["skills"] and "French" not in got["skills"]
    assert "AutoCAD" in got["skills"] and "Python" in got["skills"]


def test_heuristic_skill_boundaries():
    text = ("Alex C. Example\nPROJECTS\nTalked to C-level executives after our Series C. Used CAD tools, "
            "Objective-C and C# with ASP.NET; wrote C++17 and HTML5. You can bus tables. Excel at teamwork.\n")
    skills = P.heuristic_extract(text)["skills"]
    assert "C" not in skills and "CAN bus" not in skills and "Excel" not in skills
    assert {"Objective-C", "C#", "C++", "HTML", ".NET"} <= set(skills)
    assert "R" not in P.heuristic_extract("PROJECTS\nR&D lab, R. Example, Section R")["skills"]
    assert "R" in P.heuristic_extract("SKILLS\nPython, R, SQL")["skills"]


def test_heuristic_french_resume_and_contact_block():
    text = """Contact
marie.exemple@example.org · linkedin.com/in/marie-exemple
MARIE-ÈVE EXEMPLE
Étudiante en génie logiciel
Montréal, QC
FORMATION
Université Exemple — Baccalauréat en génie logiciel
Septembre 2022 – Avril 2026
EXPÉRIENCE
Développeuse stagiaire, Exemple Inc., Mai 2025 – Août 2025
COMPÉTENCES
Java, Kotlin, Android, HTML/CSS, CI/CD
"""
    got = P.heuristic_extract(text)
    assert got["name"] == "Marie-Ève Exemple" and got["location"] == "Montréal, QC"
    assert got["education"] == [{"school": "Université Exemple", "degree": "Baccalauréat en génie logiciel",
                                 "field": "Génie logiciel", "graduation": "Avril 2026"}]
    assert got["experience"] == [{"title": "Développeuse stagiaire", "company": "Exemple Inc.", "start": "Mai 2025",
                                  "end": "Août 2025"}]
    assert got["skills"][:6] == ["Java", "Kotlin", "Android", "HTML", "CSS", "CI/CD"]


@pytest.mark.parametrize("junk", [None, "", 123, "\x00\x01" * 100, "é" * 5000, "A" * 200_000,
                                  "EDUCATION\n" * 500, "( ) [ ] | , ; : -" * 300])
def test_heuristic_extract_never_raises(junk):
    got = P.heuristic_extract(junk)
    assert set(got) == set(P.EXTRACT_KEYS) | {"levels"}


# --------------------------------------------------------------------------- LLM extraction

def test_llm_extract_valid_reply(resume_text):
    client = FakeClient(LLM_REPLY)
    got = P.llm_extract(resume_text, client)
    call = client.calls[0]
    assert call["max_tokens"] == 2500
    for key in P.EXTRACT_KEYS:
        assert key in call["system"]
    assert "untrusted" in call["system"] and "untrusted" in call["user"]
    assert "<<<RESUME_TEXT_START>>>" in call["user"] and "<<<RESUME_TEXT_END>>>" in call["user"]
    assert "Bachelor of Computer Science" in call["user"]
    assert got["name"] == "Alex Example" and got["graduation"] == "Apr 2027"
    assert got["skills"] == ["Python", "C++", "FreeRTOS", "STM32"]
    assert got["interests"] == ["aerospace", "embedded"]   # lower-cased, aliases folded into tags, deduped
    assert got["education"][0]["school"] == "University of Exampleton"
    assert got["links"] == ["https://github.com/alex-example"]


def test_llm_extract_truncates_input():
    client = FakeClient({})
    P.llm_extract("A" * 50_000, client)
    assert client.calls[0]["user"].count("A") <= P.LLM_TEXT_CHARS + 50


def test_llm_extract_cleans_junk_types_and_ungrounded_values(resume_text):
    reply = {"name": ["Alex"], "email": "Contact: alex@example.com (preferred)", "location": "12 Elm Street, Toronto, ON",
             "headline": "Student, call 416-555-0199", "graduation": "May 2031", "education": "University",
             "experience": [{"title": "Firmware Developer Intern", "company": "Northwind Avionics", "start": 2025,
                             "end": None, "salary": "secret"}, "junk", {"title": "Astronaut", "company": "Moonbase"}],
             "skills": ["Python", 7, None, "Quantum Teleportation"], "interests": "not a list",
             "links": ["https://github.com/alex-example", "https://evil.example/phish", "javascript:alert(1)"],
             "citizenship": "Canadian", "work_authorization": "yes"}
    got = P.llm_extract(resume_text, FakeClient(reply))
    assert set(got) == set(P.EXTRACT_KEYS)
    assert got["name"] == "" and got["email"] == "alex@example.com"
    assert got["location"] == "Toronto, ON"
    assert "555" not in got["headline"]
    assert got["graduation"] == ""                      # 2031 never appears in the resume
    assert got["education"] == []
    assert got["experience"] == [{"title": "Firmware Developer Intern", "company": "Northwind Avionics",
                                  "start": "2025", "end": ""}]
    assert got["skills"] == ["Python"]
    assert got["interests"] == []
    assert got["links"] == ["https://github.com/alex-example"]


@pytest.mark.parametrize("reply", [["not", "a", "dict"], "text", None, 42])
def test_llm_extract_non_dict_reply_raises(reply):
    with pytest.raises(ValueError):
        P.llm_extract("Alex Example resume", FakeClient(reply))


def test_llm_extract_unwraps_single_nested_object(resume_text):
    got = P.llm_extract(resume_text, FakeClient({"profile": {"name": "Alex Example", "skills": ["C++"]}}))
    assert got["name"] == "Alex Example" and got["skills"] == ["C++"]


# --------------------------------------------------------------------------- merge

def test_merge_into_empty_manual_profile_takes_extracted():
    extracted = {"name": "Alex Example", "skills": ["Python"], "education": [{"school": "University of Exampleton"}]}
    merged = P.merge_profiles(P.empty_profile(), extracted)
    assert merged["name"] == "Alex Example" and merged["skills"] == ["Python"]
    assert merged["education"][0]["school"] == "University of Exampleton"
    assert merged["source"] == "manual" and merged["levels"] == list(P.LEVELS)


def test_merge_keeps_existing_values_and_unions_lists():
    base = P.normalize_profile({
        "name": "Alex E.", "email": "", "skills": ["Rust", "Python"], "interests": ["robotics"],
        "levels": ["internship"], "exclude_keywords": ["sales"], "preferred_locations": ["Toronto"],
        "education": [{"school": "University of Exampleton", "degree": "BCS"}],
        "experience": [], "links": ["https://github.com/alex-example"], "source": "resume"})
    extracted = {"name": "Alex Example", "email": "alex@example.com", "skills": ["python", "C++"],
                 "interests": ["embedded", "Robotics"], "links": ["https://github.com/alex-example/",
                                                                  "https://linkedin.com/in/alex-example"],
                 "education": [{"school": "university of exampleton", "degree": "bcs"},
                               {"school": "Exampleton College", "degree": "Diploma"}],
                 "experience": [{"title": "Intern", "company": "Northwind Avionics"}], "levels": ["new_grad"]}
    merged = P.merge_profiles(base, extracted)
    assert merged["name"] == "Alex E."                         # never overwritten
    assert merged["email"] == "alex@example.com"               # empty field filled
    assert merged["skills"] == ["Rust", "Python", "C++"]
    assert merged["interests"] == ["robotics", "embedded"]
    assert merged["links"] == ["https://github.com/alex-example", "https://linkedin.com/in/alex-example"]
    assert [e["school"] for e in merged["education"]] == ["University of Exampleton", "Exampleton College"]
    assert merged["experience"] == [{"title": "Intern", "company": "Northwind Avionics", "start": "", "end": ""}]
    assert merged["levels"] == ["internship"] and merged["exclude_keywords"] == ["sales"]
    assert merged["source"] == "resume"


def test_merge_manual_profile_with_content_is_not_replaced():
    base = P.normalize_profile({"skills": ["Go"], "source": "manual"})
    merged = P.merge_profiles(base, {"name": "Alex Example", "skills": ["Python"]})
    assert merged["skills"] == ["Go", "Python"] and merged["name"] == "Alex Example"


# --------------------------------------------------------------------------- process_resume

def test_process_resume_heuristics_only(tmp_path, resume_pdf):
    client = FakeClient(LLM_REPLY)
    result = P.process_resume(tmp_path, resume_pdf, "../../etc/My Résumé\x00.pdf", client=client, use_llm="off")
    assert set(result) == {"profile", "resume", "extracted", "warnings"}
    assert client.calls == []
    saved = result["profile"]
    assert saved["source"] == "resume" and saved["name"] == "Alex Example" and saved["updated_at"]
    assert saved == P.load_profile(tmp_path)
    info = result["resume"]
    assert info["method"] == "heuristic" and info["pages"] == 1 and info["chars"] > 200
    assert info["filename"] == "My Résumé.pdf" and "/" not in info["filename"]
    assert info["pdf"]["title"] == "Alex Example Resume"
    assert P.load_resume(tmp_path) == info and "text" not in info
    stored = json.loads((tmp_path / "resume.json").read_text())
    assert stored["text"].startswith("ALEX EXAMPLE")
    assert "555" not in json.dumps(result["profile"])


def test_process_resume_auto_uses_client_and_merges(tmp_path, resume_pdf):
    P.save_profile(tmp_path, {"skills": ["Rust"], "levels": ["internship"], "exclude_keywords": ["sales"]})
    client = FakeClient(LLM_REPLY)
    result = P.process_resume(tmp_path, resume_pdf, "cv.pdf", client=client, use_llm="auto")
    assert len(client.calls) == 1
    saved = result["profile"]
    assert saved["source"] == "resume+llm" and result["resume"]["method"] == "llm"
    assert saved["skills"][0] == "Rust" and {"Python", "C++", "KiCad"} <= set(saved["skills"])
    assert saved["levels"] == ["internship"] and saved["exclude_keywords"] == ["sales"]
    assert saved["interests"] == ["aerospace", "embedded"]
    assert result["extracted"]["headline"] == "Embedded software student"   # LLM value preferred
    assert result["warnings"] == []


def test_process_resume_auto_without_client_uses_heuristics(tmp_path, resume_pdf):
    result = P.process_resume(tmp_path, resume_pdf, "cv.pdf", client=None, use_llm="auto")
    assert result["resume"]["method"] == "heuristic" and result["profile"]["source"] == "resume"


def test_process_resume_llm_failure_falls_back(tmp_path, resume_pdf):
    client = FakeClient(error=RuntimeError("provider timed out\nTraceback details"))
    result = P.process_resume(tmp_path, resume_pdf, "cv.pdf", client=client, use_llm="on")
    assert result["warnings"] == ["LLM extraction failed: provider timed out"]
    assert result["resume"]["warnings"] == result["warnings"]
    assert result["resume"]["method"] == "heuristic" and result["profile"]["source"] == "resume"
    assert result["profile"]["name"] == "Alex Example"
    bad_reply = P.process_resume(tmp_path, resume_pdf, "cv.pdf", client=FakeClient(["list"]), use_llm="on")
    assert bad_reply["warnings"][0].startswith("LLM extraction failed:")


def test_process_resume_on_without_client_errors(tmp_path, resume_pdf):
    with pytest.raises(P.ResumeError, match="No LLM provider is configured"):
        P.process_resume(tmp_path, resume_pdf, "cv.pdf", client=None, use_llm="on")
    assert not (tmp_path / "profile.json").exists()


def test_process_resume_rejects_bad_pdf_without_writing(tmp_path):
    with pytest.raises(P.ResumeError, match="Not a PDF file"):
        P.process_resume(tmp_path, b"not a pdf", "cv.pdf")
    assert not (tmp_path / "profile.json").exists() and not (tmp_path / "resume.json").exists()


def test_process_resume_scanned_pdf_warns_and_skips_llm(tmp_path):
    client = FakeClient(LLM_REPLY)
    result = P.process_resume(tmp_path, make_pdf([[]]), "scan.pdf", client=client, use_llm="auto")
    assert P.LITTLE_TEXT_WARNING in result["warnings"] and client.calls == []
    assert result["resume"]["chars"] == 0


def test_sanitize_filename():
    assert P.sanitize_filename("C:\\Users\\alex\\resume final.pdf") == "resume final.pdf"
    assert P.sanitize_filename("") == "resume.pdf"
    assert P.sanitize_filename("<script>.pdf") == "_script_.pdf"
    long = P.sanitize_filename("a" * 300 + ".pdf")
    assert len(long) <= 120 and long.endswith(".pdf")
