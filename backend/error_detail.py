"""What a client may be told about a failure (#2297).

An exception's text belongs in the server log: a database error carries its SQL
and bound parameters, a network error the internal hosts and proxy it went
through. A message that reaches a client names the kind of failure instead.
"""

import httpx
import requests

_NETWORK_ERRORS = (
    requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
    httpx.RequestError, ConnectionError, TimeoutError,
)


def failure_kind(error: BaseException, shown: tuple = ()) -> str:
    """The kind of failure *error* is, for a message a client sees.

    Exception types in *shown* are written for the reader (DriftCheckUnavailable
    says what GitHub answered), so their own message is returned instead.
    """
    if isinstance(error, shown):
        return str(error)
    return "network error" if isinstance(error, _NETWORK_ERRORS) else "unexpected error"
