"""Validate bounded wire bodies before allowing HTTPX's automatic decoding."""
import math
import re
import time
import zlib

import httpx


class ResponseBoundError(RuntimeError):
    """A response cannot be read within the validated resource bounds."""


def read_bounded(response: httpx.Response, *, max_bytes: int, deadline: float | None = None) -> bytes:
    """Consume/close an unread sync response and return its validated WIRE bytes.

    Both wire and decoded sizes must fit max_bytes. Inflate with one bounded
    overflow byte, never zlib.flush (its length is not an allocation limit).
    The absolute monotonic deadline complements, not replaces, socket timeouts.
    """
    def check_time():
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponseBoundError("Response deadline exceeded")

    if not isinstance(response.stream, httpx.SyncByteStream):
        raise ResponseBoundError("An unread synchronous response is required")
    try:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ResponseBoundError("Invalid response byte limit")
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ResponseBoundError("Invalid response deadline")
        check_time()
        if response.is_stream_consumed or response.is_closed:
            raise ResponseBoundError("An unread response is required; buffered bodies are not accepted")
        limit_error = f"Response exceeds {max_bytes} byte limit"
        lengths = response.headers.get_list("Content-Length")
        length = None
        if lengths:
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0].strip(" \t"))
                    or "Transfer-Encoding" in response.headers):
                raise ResponseBoundError("Invalid or ambiguous Content-Length")
            try:
                length = int(lengths[0])
            except ValueError as exc:
                raise ResponseBoundError("Invalid Content-Length") from exc
            if length > max_bytes:
                raise ResponseBoundError(limit_error)
        codings = response.headers.get_list("Content-Encoding")
        coding = codings[0].strip(" \t").lower() if codings else "identity"
        if len(codings) > 1 or coding not in {"identity", "gzip", "deflate"}:
            raise ResponseBoundError("Unsupported or stacked Content-Encoding")
        decoder = None if coding == "identity" else zlib.decompressobj(31 if coding == "gzip" else 15)
        raw_fallback = coding == "deflate"
        body, decoded_size = bytearray(), 0
        # iter_raw filters empty chunks; inspect the transport itself so even
        # empty raw chunks cannot evade the before/after deadline checks.
        response.is_stream_consumed = True
        chunks = iter(response.stream)
        while True:
            check_time()
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            check_time()
            size = len(body) + len(chunk)
            if size > max_bytes:
                raise ResponseBoundError(limit_error)
            if length is not None and size > length:
                raise ResponseBoundError("Response Content-Length mismatch")
            body.extend(chunk)
            if decoder is not None:
                try:
                    try:
                        decoded = decoder.decompress(chunk, max_bytes - decoded_size + 1)
                    except zlib.error:
                        if not raw_fallback:
                            raise
                        # A split zlib header may fail only on a later chunk.
                        # Retry raw DEFLATE once, replaying the bounded prefix.
                        raw_fallback, decoded_size = False, 0
                        decoder = zlib.decompressobj(-15)
                        check_time()
                        decoded = decoder.decompress(body, max_bytes + 1)
                except zlib.error as exc:
                    raise ResponseBoundError("Invalid compressed response") from exc
                check_time()
                decoded_size += len(decoded)
                if decoded_size > max_bytes:
                    raise ResponseBoundError(limit_error)
                if decoder.unused_data or decoder.unconsumed_tail:
                    raise ResponseBoundError("Invalid compressed response: trailing data")
        if length is not None and len(body) != length:
            raise ResponseBoundError("Response Content-Length mismatch")
        if decoder is not None and not decoder.eof:
            raise ResponseBoundError("Invalid compressed response: incomplete stream")
        result = bytes(body)
    finally:
        response.close()
    check_time()
    return result
