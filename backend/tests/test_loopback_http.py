from __future__ import annotations

from email.message import Message
from io import BytesIO

import pytest

from common.loopback_http import read_bounded_body


class _Connection:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value


class _TimeoutReader:
    def read(self, length: int) -> bytes:
        del length
        raise TimeoutError


def _headers(*pairs: tuple[str, str]) -> Message:
    headers = Message()
    for name, value in pairs:
        headers[name] = value
    return headers


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (_headers(), "missing"),
        (_headers(("Content-Length", "-1")), "invalid"),
        (_headers(("Content-Length", "+1")), "invalid"),
        (_headers(("Content-Length", " 1")), "invalid"),
        (
            _headers(("Content-Length", "1"), ("Content-Length", "1")),
            "invalid",
        ),
        (
            _headers(
                ("Content-Length", "1"),
                ("Transfer-Encoding", "chunked"),
            ),
            "transfer_encoding",
        ),
        (_headers(("Content-Length", "9")), "too_large"),
    ],
)
def test_read_bounded_body_rejects_ambiguous_framing(headers: Message, expected: str) -> None:
    assert read_bounded_body(headers, BytesIO(b"x"), max_bytes=8)[0] == expected


def test_read_bounded_body_requires_exact_bytes_and_sets_deadline() -> None:
    connection = _Connection()
    status, body = read_bounded_body(
        _headers(("Content-Length", "3")),
        BytesIO(b"ab"),
        max_bytes=8,
        connection=connection,
        timeout_seconds=0.25,
    )
    assert (status, body) == ("truncated", None)
    assert connection.timeout == 0.25


def test_read_bounded_body_reports_partial_body_timeout() -> None:
    status, body = read_bounded_body(
        _headers(("Content-Length", "3")),
        _TimeoutReader(),
        max_bytes=8,
        connection=_Connection(),
    )
    assert (status, body) == ("timeout", None)


def test_read_bounded_body_returns_only_an_exact_bounded_body() -> None:
    status, body = read_bounded_body(
        _headers(("Content-Length", "3")), BytesIO(b"abcNEXT"), max_bytes=8
    )
    assert (status, body) == ("ok", b"abc")
