"""Tests for the ClickHouse HTTP client.

The client is small enough to test against a recorder rather than a server, and
that is deliberate: what needs pinning is the *shape* of the request -- where the
statement goes, where the rows go, where the credentials go -- because each of
those is a place a mistake leaks data or silently writes nothing.

The one behaviour worth stating: an empty batch issues no request at all. A page
can legitimately produce no rows, and an INSERT with an empty body is a round
trip that can only fail.
"""

import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
import urllib.error
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION_DIR = os.path.dirname(HERE)
# The shared connectors live beside this job's directory, under
# shopify_integrations/lib/python. Resolving them relative to this file is what
# lets the suite run from anywhere -- a bare `python3 -m unittest discover
# -s tests`, an editor, or CI -- with no PYTHONPATH set by the caller.
LIB_PYTHON = os.path.join(os.path.dirname(INTEGRATION_DIR), "lib", "python")

for path in (LIB_PYTHON, INTEGRATION_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from otter_connectors.clickhouse import (  # noqa: E402
    USER_AGENT,
    ClickHouseClient,
    ClickHouseError,
)
from otter_connectors.errors import ConfigError  # noqa: E402


class FakeResponse:
    def __init__(self, body=b""):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    """Stands in for ``http_open``: records the request, replays a response."""

    def __init__(self, body=b"", error=None):
        self.body = body
        self.error = error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)

    @property
    def request(self):
        return self.requests[-1][0]

    def params(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.request.full_url).query)


def client(**overrides):
    settings = {
        "url": "https://clickhouse.example:8443",
        "database": "analytics",
        "username": "ingest",
        "password": "secret",
        "timeout": 30,
    }
    settings.update(overrides)
    return ClickHouseClient(**settings)


class InsertTest(unittest.TestCase):
    def test_rows_are_newline_delimited_json_in_the_body(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            written = client().insert("shopify_orders", [{"a": 1}, {"a": 2}])

        self.assertEqual(written, 2)
        self.assertEqual(recorder.request.data, b'{"a":1}\n{"a":2}')
        self.assertEqual(recorder.params()["query"], ["INSERT INTO shopify_orders FORMAT JSONEachRow"])
        self.assertEqual(recorder.params()["database"], ["analytics"])

    def test_an_empty_batch_issues_no_request(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            written = client().insert("shopify_orders", [])

        self.assertEqual(written, 0)
        self.assertEqual(recorder.requests, [])

    def test_credentials_are_headers_and_never_the_url(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().insert("t", [{"a": 1}])

        headers = {key.lower(): value for key, value in recorder.request.headers.items()}
        self.assertEqual(headers["x-clickhouse-user"], "ingest")
        self.assertEqual(headers["x-clickhouse-key"], "secret")
        self.assertNotIn("secret", recorder.request.full_url)
        self.assertNotIn("ingest", recorder.request.full_url)

    def test_a_rejection_carries_the_server_message(self):
        error = urllib.error.HTTPError(
            "https://clickhouse.example:8443",
            400,
            "Bad Request",
            {},
            io.BytesIO(b"Code: 53. Cannot parse input: expected ',' before: 'x'"),
        )
        with mock.patch("otter_connectors.clickhouse.http_open", Recorder(error=error)):
            with self.assertRaises(ClickHouseError) as caught:
                client().insert("t", [{"a": 1}])

        # The column and the value are the whole diagnosis; a generic message
        # would throw them away.
        self.assertIn("Cannot parse input", str(caught.exception))
        self.assertIn("400", str(caught.exception))

    def test_an_unreachable_server_is_a_clickhouse_error(self):
        error = urllib.error.URLError("connection refused")
        with mock.patch("otter_connectors.clickhouse.http_open", Recorder(error=error)):
            with self.assertRaises(ClickHouseError) as caught:
                client().insert("t", [{"a": 1}])
        self.assertIn("unreachable", str(caught.exception))


class QueryTest(unittest.TestCase):
    def test_a_bare_select_gets_a_format_clause(self):
        recorder = Recorder(body=json.dumps({"data": [{"count()": 3}]}).encode())
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            rows = client().query("SELECT count() FROM t")

        self.assertEqual(rows, [{"count()": 3}])
        self.assertEqual(recorder.params()["query"], ["SELECT count() FROM t FORMAT JSON"])

    def test_an_explicit_format_is_left_alone(self):
        recorder = Recorder(body=json.dumps({"data": []}).encode())
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().query("SELECT 1 FORMAT JSONEachRow")

        self.assertEqual(recorder.params()["query"], ["SELECT 1 FORMAT JSONEachRow"])

    def test_a_trailing_semicolon_does_not_break_the_format_clause(self):
        recorder = Recorder(body=json.dumps({"data": []}).encode())
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().query("SELECT 1;\n")

        self.assertEqual(recorder.params()["query"], ["SELECT 1 FORMAT JSON"])

    def test_an_empty_body_is_no_rows(self):
        with mock.patch("otter_connectors.clickhouse.http_open", Recorder(body=b"")):
            self.assertEqual(client().query("SELECT 1"), [])

    def test_a_non_json_body_is_an_error_rather_than_silence(self):
        with mock.patch("otter_connectors.clickhouse.http_open", Recorder(body=b"<html>")):
            with self.assertRaises(ClickHouseError):
                client().query("SELECT 1")


class UserAgentTest(unittest.TestCase):
    """The request has to say who it is.

    Cloudflare's browser-integrity check rejects the default ``Python-urllib/x.y``
    signature with a 403 whose body is "error code: 1010" -- before Access checks
    the service token and before ClickHouse sees the request. The failure names
    neither the token nor the database, so it reads like a credentials problem
    when the credentials are fine. This assertion is the only thing standing
    between that and a very confusing 403.
    """

    def test_a_named_user_agent_is_sent(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().insert("t", [{"a": 1}])

        headers = {key.lower(): value for key, value in recorder.request.headers.items()}
        self.assertEqual(headers["user-agent"], USER_AGENT)

    def test_the_default_urllib_agent_is_not_sent(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().query("SELECT 1")

        sent = {key.lower(): value for key, value in recorder.request.headers.items()}["user-agent"]
        self.assertNotIn("python-urllib", sent.lower())


class RowEncodingTest(unittest.TestCase):
    """What a mapping produces has to become bytes the server accepts.

    ``json`` has no ``Decimal`` and no ``datetime``, so how those two cross the
    wire is a decision this client makes. Both choices exist because the mapping
    deliberately keeps the source's exact types instead of flattening them.
    """

    def test_a_decimal_is_sent_as_a_quoted_string(self):
        # ClickHouse parses the digits straight into Decimal(18,2). A bare number
        # would work for two-decimal money, but only because those values happen
        # to survive a float round trip -- the string does not depend on that.
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().insert("t", [{"amount": Decimal("19.99")}])

        self.assertEqual(recorder.request.data, b'{"amount":"19.99"}')

    def test_a_decimal_keeps_digits_a_float_would_lose(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().insert("t", [{"amount": Decimal("0.1000000000000000055511151231257827")}])

        self.assertIn(b"0.1000000000000000055511151231257827", recorder.request.data)

    def test_a_datetime_is_sent_as_iso_utc(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().insert("t", [{"at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)}])

        self.assertEqual(recorder.request.data, b'{"at":"2026-01-02T03:04:05Z"}')

    def test_an_unsupported_type_is_refused_rather_than_coerced(self):
        # A set in a row is a mapping bug. Stringifying it would write something
        # plausible and wrong, which is worse than a failed run.
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            with self.assertRaises(TypeError):
                client().insert("t", [{"tags": {"trail", "mug"}}])


class AccessTokenTest(unittest.TestCase):
    """A tunnelled endpoint sits behind Cloudflare Access.

    Access refuses the request before ClickHouse ever sees it, so a missing
    service token surfaces as a 403 that says nothing about the cause. These pin
    where the token goes, and that half a token fails here rather than at request
    time with that opaque 403.
    """

    def test_no_token_sends_no_access_headers(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client().insert("t", [{"a": 1}])

        headers = {key.lower() for key in recorder.request.headers}
        self.assertNotIn("cf-access-client-id", headers)
        self.assertNotIn("cf-access-client-secret", headers)

    def test_a_token_is_sent_as_access_headers(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client(access_client_id="token-id", access_client_secret="token-secret").insert(
                "t", [{"a": 1}]
            )

        headers = {key.lower(): value for key, value in recorder.request.headers.items()}
        self.assertEqual(headers["cf-access-client-id"], "token-id")
        self.assertEqual(headers["cf-access-client-secret"], "token-secret")

    def test_the_token_never_reaches_the_url(self):
        recorder = Recorder()
        with mock.patch("otter_connectors.clickhouse.http_open", recorder):
            client(access_client_id="token-id", access_client_secret="token-secret").insert(
                "t", [{"a": 1}]
            )

        self.assertNotIn("token-id", recorder.request.full_url)
        self.assertNotIn("token-secret", recorder.request.full_url)

    def test_half_a_token_is_rejected_before_any_request(self):
        for only in ({"access_client_id": "token-id"}, {"access_client_secret": "token-secret"}):
            with self.subTest(only=only):
                with self.assertRaises(ConfigError) as caught:
                    client(**only)
                self.assertIn("both", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
