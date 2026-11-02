"""Shared HTTP reader contract: in-memory unread transport streams only."""
import gzip
import time
import zlib

import httpx
import pytest

from career_radar.bounded_http import ResponseBoundError, read_bounded


def unread_response(*args, **kwargs):
    """Turn a synthetic JSON/text fixture into an unread transport response."""
    fixture = httpx.Response(*args, **kwargs)
    return httpx.Response(fixture.status_code, headers=fixture.headers,
                          stream=httpx.ByteStream(fixture.content))


def compressed(body, coding):
    if coding == "gzip":
        return gzip.compress(body)
    if coding == "deflate":
        return zlib.compress(body)
    if coding == "raw-deflate":
        encoder = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return encoder.compress(body) + encoder.flush()
    return body


def coding_headers(coding):
    return {"Content-Encoding": "deflate" if coding == "raw-deflate" else coding}


def inflated_header(extra_size=256):
    """Valid gzip whose optional header produces no decoded output."""
    wire = bytearray(gzip.compress(b"{}"))
    wire[3] = 4  # FEXTRA
    return bytes(wire[:10]) + extra_size.to_bytes(2, "little") + b"x" * extra_size + bytes(wire[10:])


class ProbeStream(httpx.SyncByteStream):
    def __init__(self, chunks, on_chunk=None, on_end=None):
        self.chunks, self.on_chunk, self.on_end = chunks, on_chunk, on_end
        self.consumed, self.closed = [], False

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            self.consumed.append(index)
            if self.on_chunk:
                self.on_chunk(index)
            yield chunk
        if self.on_end:
            self.on_end()

    def close(self):
        self.closed = True


def test_shared_reader_returns_wire_bytes_and_closes_stream():
    stream = ProbeStream([b'one', b'two'])
    response = httpx.Response(200, stream=stream)
    assert read_bounded(response, max_bytes=6) == b'onetwo'
    assert stream.consumed == [0, 1] and stream.closed
    assert response.is_closed and response.is_stream_consumed


@pytest.mark.parametrize("headers", [{}, {"Transfer-Encoding": "chunked"}, {"Content-Length": " \t0006\t "}])
def test_exact_limit_and_chunked_lengths(headers):
    stream = ProbeStream([b"abc", b"def"])
    assert read_bounded(httpx.Response(200, headers=headers, stream=stream), max_bytes=6) == b"abcdef"
    assert stream.closed


@pytest.mark.parametrize("value", ["-1", "+2", "2.0", "1_0", "2, 2", "", "NaN", "\v2", "2\r", "2\n", pytest.param("9" * 5000, id="too-long")])
def test_invalid_lengths_fail_before_reading(value):
    stream = ProbeStream([b"never-read"])
    with pytest.raises(ResponseBoundError, match="Content-Length|byte limit"):
        read_bounded(httpx.Response(200, headers={"Content-Length": value}, stream=stream), max_bytes=16)
    assert not stream.consumed and stream.closed


@pytest.mark.parametrize("headers", [
    [("Content-Length", "2"), ("Content-Length", "2")],
    {"Content-Length": "2", "Transfer-Encoding": "chunked"},
    {"Content-Length": "17"},
])
def test_ambiguous_or_oversized_lengths_fail_without_reading(headers):
    stream = ProbeStream([b"never-read"])
    with pytest.raises(ResponseBoundError):
        read_bounded(httpx.Response(200, headers=headers, stream=stream), max_bytes=16)
    assert not stream.consumed and stream.closed


@pytest.mark.parametrize("length,chunks,seen", [
    (None, [b"x" * 10, b"y" * 10, b"never-read"], [0, 1]),
    ("2", [b"{}", b"extra", b"never-read"], [0, 1]),
    ("0", [b"extra", b"never-read"], [0]),
    ("3", [b"{}"], [0]),
])
def test_wire_overflow_or_length_mismatch_stops_and_closes(length, chunks, seen):
    stream = ProbeStream(chunks)
    headers = {} if length is None else {"Content-Length": length}
    with pytest.raises(ResponseBoundError):
        read_bounded(httpx.Response(200, headers=headers, stream=stream), max_bytes=16)
    assert stream.consumed == seen and stream.closed


@pytest.mark.parametrize("coding", ["identity", "gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("single_byte_chunks", [False, True])
def test_returns_original_wire_and_preserves_html_charset_headers(coding, single_byte_chunks):
    body = b"<p>caf\xe9 &amp; th\xe9</p>" * 8
    wire = compressed(body, coding)
    chunks = [wire[i:i + 1] for i in range(len(wire))] if single_byte_chunks else [wire]
    headers = coding_headers(coding) | {"Content-Length": str(len(wire)),
        "Content-Type": "text/html; charset=iso-8859-1", "Link": '</jobs?page=2>; rel="next"'}
    stream = ProbeStream(chunks)
    original = httpx.Response(200, headers=headers, stream=stream,
                              request=httpx.Request("GET", "https://fixture.example/jobs"))
    validated = read_bounded(original, max_bytes=max(len(wire), len(body)))
    assert validated == wire and stream.closed
    result = httpx.Response(original.status_code, headers=original.headers, content=validated,
                            request=original.request, default_encoding=original.default_encoding)
    assert result.content == body and result.text == body.decode("iso-8859-1")
    assert result.headers == original.headers and result.links == original.links and result.url == original.url


@pytest.mark.parametrize("coding", ["br", "zstd", "", "gzip,", "gzip, deflate", "gzip, identity", "identity, gzip", "gzip, gzip", "gzip\n", "\vgzip", "identity\r"])
def test_unknown_or_stacked_codings_are_rejected_before_read(coding):
    stream = ProbeStream([b"never-read"])
    with pytest.raises(ResponseBoundError, match="Content-Encoding"):
        read_bounded(httpx.Response(200, headers={"Content-Encoding": coding}, stream=stream), max_bytes=1024)
    assert not stream.consumed and stream.closed


@pytest.mark.parametrize("coding", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("damage", ["invalid", "truncated", "trailing", "second-member", "empty"])
def test_compressed_corruption_cannot_be_a_success(coding, damage):
    wire = compressed(b"{}", coding)
    chunks = {"invalid": [b"invalid"], "truncated": [wire[:-1]], "trailing": [wire, b"\x00"],
              "second-member": [wire + wire], "empty": []}[damage]
    stream = ProbeStream(chunks)
    with pytest.raises(ResponseBoundError, match="compressed"):
        read_bounded(httpx.Response(200, headers=coding_headers(coding), stream=stream), max_bytes=1024)
    assert stream.closed


@pytest.mark.parametrize("coding", ["gzip", "deflate", "raw-deflate"])
def test_each_decompression_allocation_has_a_hard_output_bound(monkeypatch, coding):
    original = zlib.decompressobj
    allocations = []

    class Inflater:
        def __init__(self, *args):
            self.real = original(*args)

        def decompress(self, data, max_length=0):
            assert 0 < max_length <= 1025
            value = self.real.decompress(data, max_length)
            allocations.append(len(value))
            return value

        def flush(self, *args):
            pytest.fail("zlib.flush's length is not an allocation bound")

        def __getattr__(self, key):
            return getattr(self.real, key)

    stream = ProbeStream([compressed(b"x" * 65536, coding), b"never-read"])
    monkeypatch.setattr(zlib, "decompressobj", Inflater)
    with pytest.raises(ResponseBoundError, match="byte limit"):
        read_bounded(httpx.Response(200, headers=coding_headers(coding), stream=stream), max_bytes=1024)
    assert allocations and max(allocations) <= 1025
    assert stream.consumed == [0] and stream.closed


def test_wire_bytes_are_bounded_even_when_gzip_emits_nothing():
    wire = inflated_header()
    stream = ProbeStream([wire[i:i + 32] for i in range(0, len(wire), 32)])
    with pytest.raises(ResponseBoundError, match="byte limit"):
        read_bounded(httpx.Response(200, headers=coding_headers("gzip"), stream=stream), max_bytes=64)
    assert stream.consumed == [0, 1, 2] and stream.closed


@pytest.mark.parametrize("first,coding", [(b"", "identity"), (b"x", "identity"), (b"\x1f", "gzip")])
def test_deadline_is_checked_after_every_raw_chunk_even_without_output(monkeypatch, first, coding):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    stream = ProbeStream([first, b"never-read"], on_chunk=lambda i: clock.__setitem__(0, 2.0))
    with pytest.raises(ResponseBoundError, match="deadline"):
        read_bounded(httpx.Response(200, headers=coding_headers(coding), stream=stream), max_bytes=1024, deadline=1.0)
    assert stream.consumed == [0] and stream.closed


def test_expired_deadline_prevents_first_read(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)
    stream = ProbeStream([b"never-read"])
    with pytest.raises(ResponseBoundError, match="deadline"):
        read_bounded(httpx.Response(200, stream=stream), max_bytes=16, deadline=1.0)
    assert not stream.consumed and stream.closed


@pytest.mark.parametrize("chunks", [[], [b"{}"]])
def test_deadline_is_checked_after_eof_and_before_return(monkeypatch, chunks):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    stream = ProbeStream(chunks, on_end=lambda: clock.__setitem__(0, 2.0))
    with pytest.raises(ResponseBoundError, match="deadline"):
        read_bounded(httpx.Response(200, stream=stream), max_bytes=16, deadline=1.0)
    assert stream.closed


def test_final_deadline_includes_stream_close(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class ClosingStream(ProbeStream):
        def close(self):
            super().close()
            clock[0] = 2.0

    stream = ClosingStream([b"{}"])
    with pytest.raises(ResponseBoundError, match="deadline"):
        read_bounded(httpx.Response(200, stream=stream), max_bytes=16, deadline=1.0)
    assert stream.closed


@pytest.mark.parametrize("body", [b"", b"{}"])
def test_buffered_responses_have_no_compatibility_fallback(body):
    with pytest.raises(ResponseBoundError, match="unread"):
        read_bounded(httpx.Response(200, content=body), max_bytes=16)


def test_empty_response_fits_zero_bytes():
    stream = ProbeStream([])
    assert read_bounded(httpx.Response(200, stream=stream), max_bytes=0) == b""
    assert stream.closed


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_invalid_byte_budgets_fail_before_reading(limit):
    stream = ProbeStream([b"never-read"])
    with pytest.raises(ResponseBoundError):
        read_bounded(httpx.Response(200, stream=stream), max_bytes=limit)
    assert not stream.consumed and stream.closed


@pytest.mark.parametrize("deadline", [float("nan"), float("inf")])
def test_nonfinite_deadlines_cannot_disable_budget(deadline):
    stream = ProbeStream([b"never-read"])
    with pytest.raises(ResponseBoundError):
        read_bounded(httpx.Response(200, stream=stream), max_bytes=16, deadline=deadline)
    assert not stream.consumed and stream.closed
