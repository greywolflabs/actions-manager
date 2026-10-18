"""Invalid ruleset input is the client's error, not a server fault (#2239).

Upload and create caught every exception as a 500 carrying the raw exception
text, so a null or over-long name, or a file that is not UTF-8, came back as a
server error quoting the database, and a non-string name could be stored as-is.
"""
import io
import json

import pytest
from fastapi.testclient import TestClient

from main import app
from models import Account, Project, Ruleset
from tests.conftest import TestingSessionLocal

OWNER = "rs-input-owner"
PROJECT = "InputProject"

client = TestClient(app)


@pytest.fixture
def owner(authenticate_client):
    db = TestingSessionLocal()
    try:
        account = Account(github_user=OWNER, github_email=f"{OWNER}@example.com", account_type="free")
        db.add(account)
        db.flush()
        db.add(Project(project_name=PROJECT, project_code="RSIN", user_id=account.user_id, project_type="standard"))
        db.commit()
    finally:
        db.close()
    authenticate_client(client, OWNER, TestingSessionLocal)


def _upload(content: bytes, filename="ruleset.json"):
    return client.post(
        "/api/rulesets/upload",
        data={"project_name": PROJECT, "github_user": OWNER},
        files={"file": (filename, io.BytesIO(content), "application/json")},
    )


def _create(body):
    return client.post("/api/rulesets/create", params={"github_user": OWNER}, json=body)


def _stored() -> int:
    db = TestingSessionLocal()
    try:
        return db.query(Ruleset).count()
    finally:
        db.close()


@pytest.mark.parametrize(
    "ruleset",
    [
        {"name": None},
        {"name": 123},
        {"name": "   "},
        {"name": "x" * 256},
        {"name": "ok", "description": "d" * 501},
        {"name": "ok", "description": ["not", "a", "string"]},
        {"name": "\ud800"},
        {"name": "a\x00b"},
        {"name": "ok", "description": "\udfff"},
    ],
    ids=[
        "null-name", "non-string-name", "blank-name", "long-name", "long-description",
        "non-string-description", "surrogate-name", "nul-name", "surrogate-description",
    ],
)
def test_upload_rejects_fields_the_table_cannot_store(owner, ruleset):
    resp = _upload(json.dumps(ruleset).encode())

    assert resp.status_code == 400, resp.text
    assert _stored() == 0


@pytest.mark.parametrize(
    "content",
    [
        b'{"name": "\xff"}',
        b"[" * 100_000 + b"]" * 100_000,
        b'{"name": "x", "n": ' + b"9" * 5000 + b"}",
    ],
    ids=["not-utf8", "deeply-nested", "huge-number"],
)
def test_upload_rejects_a_file_json_cannot_read(owner, content):
    resp = _upload(content)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Invalid JSON file"
    assert _stored() == 0


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf" + json.dumps({"name": "bom"}).encode(),
        json.dumps({"name": "bom"}).encode("utf-16"),
    ],
    ids=["utf8-bom", "utf16"],
)
def test_upload_accepts_json_saved_with_a_bom_or_as_utf16(owner, content):
    resp = _upload(content)

    assert resp.status_code == 200, resp.text
    assert resp.json()["ruleset_name"] == "bom"


def test_a_long_filename_does_not_fail_the_upload(owner):
    """The fallback description quotes the filename; that is ours to fit, not the client's."""
    resp = _upload(json.dumps({"name": "n"}).encode(), filename="y" * 490 + ".json")

    assert resp.status_code == 200, resp.text


def test_only_the_extension_is_dropped_from_a_filename_name(owner):
    resp = _upload(json.dumps({"target": "branch"}).encode(), filename="a.json.json")

    assert resp.json()["ruleset_name"] == "a.json"


def test_upload_accepts_limits_exactly(owner):
    resp = _upload(json.dumps({"name": "x" * 255, "description": "d" * 500}).encode())

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "ruleset_json": {}},
        {"name": "x" * 256, "ruleset_json": {}},
        {"name": "ok", "description": "d" * 501, "ruleset_json": {}},
        {"name": "   ", "ruleset_json": {}},
        {"name": "a\x00b", "ruleset_json": {}},
    ],
    ids=["empty-name", "long-name", "long-description", "blank-name", "nul-name"],
)
def test_create_rejects_fields_the_table_cannot_store(owner, body):
    resp = _create(body)

    assert resp.status_code == 422, resp.text
    assert _stored() == 0


@pytest.mark.parametrize("route", ["upload", "create"])
def test_a_server_error_does_not_echo_the_exception(owner, route, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("internal detail that must not reach the client")

    monkeypatch.setattr("rulesets.Ruleset", fail)

    if route == "upload":
        resp = _upload(json.dumps({"name": "ok"}).encode())
    else:
        resp = _create({"name": "ok", "ruleset_json": {}})

    assert resp.status_code == 500
    assert "internal detail" not in resp.text
