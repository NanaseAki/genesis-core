"""Bounded response framing for one Connection: close exchange (CPython 3.12+).

Read HTTPResponse.fp directly, never its permissive chunk decoder. The caller
owns/closes the response. No payload is attached to framing exceptions.
"""
import http.client
import re
from typing import BinaryIO

LINE_LIMIT = 4096
CONTROL_LIMIT = 65536
READ_SIZE = 65536
TOKEN = rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
QUOTED = rb'"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*"'
CHUNK_LINE = re.compile(rb"([0-9A-Fa-f]{1,16})(?:[ \t]*;[ \t]*" + TOKEN
                        + rb"(?:[ \t]*=[ \t]*(?:" + TOKEN + rb"|" + QUOTED + rb"))?)*\r\n")


class FramingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _exact(stream: BinaryIO, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        piece = stream.read(min(READ_SIZE, size - len(output)))
        if not piece:
            raise FramingError("incomplete_response")
        output.extend(piece)
    return bytes(output)


def _end(stream: BinaryIO) -> None:
    # We explicitly requested connection closure. EOF is required, so delayed
    # trailing bytes cannot masquerade as another response or be ignored.
    if stream.read(1):
        raise FramingError("invalid_response")


def read_body(response: http.client.HTTPResponse, limit: int) -> bytes:
    lengths = response.headers.get_all("Content-Length", [])
    transfers = response.headers.get_all("Transfer-Encoding", [])
    if (response.headers.defects or len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers)
            or response.headers.get_all("Trailer", [])):
        raise FramingError("invalid_response")
    stream = response.fp
    if stream is None:
        raise FramingError("incomplete_response")
    if transfers:
        if transfers[0].strip(" \t").lower() != "chunked":
            raise FramingError("invalid_response")
        output = bytearray()
        control = 0
        while True:
            budget = min(LINE_LIMIT, CONTROL_LIMIT - control)
            if budget <= 0:
                raise FramingError("invalid_response")
            line = stream.readline(budget + 1)
            if len(line) > budget:
                raise FramingError("invalid_response")
            control += len(line)
            if not line.endswith(b"\r\n"):
                raise FramingError("incomplete_response")
            match = CHUNK_LINE.fullmatch(line)
            if match is None:
                raise FramingError("invalid_response")
            size = int(match[1], 16)
            if size > limit - len(output):
                raise FramingError("response_too_large")
            if control + 2 > CONTROL_LIMIT:
                raise FramingError("invalid_response")
            if size == 0:
                # No trailers are supported, including undeclared trailers.
                if _exact(stream, 2) != b"\r\n":
                    raise FramingError("invalid_response")
                _end(stream)
                return bytes(output)
            output.extend(_exact(stream, size))
            if _exact(stream, 2) != b"\r\n":
                raise FramingError("invalid_response")
            control += 2
    if lengths:
        value = lengths[0].strip(" \t")
        if not value.isascii() or not value.isdecimal() or len(value) > 20:
            raise FramingError("invalid_response")
        size = int(value)
        if size > limit:
            raise FramingError("response_too_large")
        output = _exact(stream, size)
        _end(stream)
        return output
    output = bytearray()
    while True:
        piece = stream.read(min(READ_SIZE, limit - len(output) + 1))
        if not piece:
            return bytes(output)
        if len(piece) > limit - len(output):
            raise FramingError("response_too_large")
        output.extend(piece)
