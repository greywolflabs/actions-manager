"""failure_kind names a failure without repeating its text (#2297)."""

import httpx
import requests
from sqlalchemy.exc import OperationalError

from error_detail import failure_kind

MARKER = "secret-marker-2297"


class Shown(Exception):
    """Stands in for an exception whose message is written for the reader."""


def test_a_network_error_is_named_without_its_text():
    for error in (
        requests.ConnectionError(f"proxy {MARKER}.internal refused"),
        requests.Timeout(MARKER),
        requests.exceptions.ChunkedEncodingError(f"Connection broken: {MARKER}"),
        httpx.ConnectError(f"proxy {MARKER}.internal refused"),
        ConnectionError(MARKER),
        TimeoutError(MARKER),
    ):
        assert failure_kind(error) == "network error"


def test_anything_else_is_an_unexpected_error():
    for error in (OperationalError(f"SELECT {MARKER}", {}, Exception(MARKER)), RuntimeError(MARKER)):
        assert failure_kind(error) == "unexpected error"


def test_a_shown_type_keeps_its_own_message():
    assert failure_kind(Shown("GitHub returned 403 for o/r"), (Shown,)) == "GitHub returned 403 for o/r"
    assert failure_kind(RuntimeError(MARKER), (Shown,)) == "unexpected error"
