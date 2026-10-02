"""A ClickHouse client over the HTTP interface, standard library only.

ClickHouse's HTTP interface takes the statement as a query parameter and, for an
insert, the rows as the request body. That is all these integrations need --
insert ``JSONEachRow``, read ``JSON`` -- so there is no driver dependency and
nothing to source a wheel for on the prepared interpreter.

**Idempotency is the destination's job, not this client's.** The tables these
integrations write are ``ReplacingMergeTree`` keyed by the source's stable id, so
re-sending a page after a crash converges instead of duplicating. The client
deliberately does not dedupe, buffer, or retry: a failed insert raises, the run
fails, and Otter retries it with the same rows.

Rows are dicts. Values must already be JSON-serializable in a shape ClickHouse
can read: ``str`` for String/DateTime/UUID, ``int``/``float`` for the numeric
types, ``bool`` for Bool, ``list`` for Array and ``None`` for Nullable.
"""

import datetime
import decimal
import json
import urllib.error
import urllib.parse
import urllib.request

from .errors import ConfigError, ConnectorError
from .http import http_open

__all__ = ["ClickHouseClient", "ClickHouseError", "USER_AGENT"]

#: The User-Agent this client identifies as.
#:
#: This is not cosmetic. Cloudflare's browser-integrity check rejects the default
#: ``Python-urllib/x.y`` signature with its own 403 -- body "error code: 1010" --
#: *before* Cloudflare Access evaluates the service token and before ClickHouse
#: sees anything. The failure names neither the token nor the database, so it
#: reads like a credentials problem when the credentials are fine. Naming the
#: client is more honest than impersonating a browser or a driver.
USER_AGENT = "otter-clickhouse/0.1.0"


class ClickHouseError(ConnectorError):
    """ClickHouse rejected a statement.

    The server's message names the column and the offending value, so it is
    carried through verbatim rather than replaced with something generic --
    "Cannot parse input: expected ..." is the whole diagnosis.
    """


class ClickHouseClient:
    """One ClickHouse database, reached over HTTP.

    ``url`` is the scheme, host and port only (``https://abc.clickhouse.cloud:8443``),
    because the database is a query parameter and the username and password are
    headers. Keeping credentials out of the URL is what stops them appearing in a
    log line or an error message that quotes the URL.

    ``access_client_id`` and ``access_client_secret`` are a Cloudflare Access
    service token, for an endpoint published through a tunnel rather than reached
    directly. Access stands in front of ClickHouse and refuses an unauthenticated
    request before it arrives, so without the token the failure looks like a 403
    from Cloudflare rather than anything to do with the database.

    They are a pair. Setting one without the other is rejected here rather than
    at request time, because the symptom would be an opaque 403 that says nothing
    about which half is missing.
    """

    def __init__(self, url, database="default", username=None, password=None,
                 access_client_id=None, access_client_secret=None, timeout=60):
        if bool(access_client_id) != bool(access_client_secret):
            raise ConfigError(
                "a Cloudflare Access service token needs both halves: set both "
                "CLICKHOUSE_ACCESS_CLIENT_ID and CLICKHOUSE_ACCESS_CLIENT_SECRET, or neither"
            )
        self.url = url.rstrip("/")
        self.database = database
        self.username = username
        self.password = password
        self.access_client_id = access_client_id
        self.access_client_secret = access_client_secret
        self.timeout = timeout

    def insert(self, table, rows):
        """Insert ``rows`` into ``table`` and return how many were sent.

        ``JSONEachRow`` is one JSON object per line. An empty batch is a no-op
        rather than an error: a page can legitimately produce no records, and
        issuing an empty INSERT would only be a round trip.
        """
        rows = list(rows)
        if not rows:
            return 0

        body = "\n".join(_encode(row) for row in rows)
        self._post("INSERT INTO %s FORMAT JSONEachRow" % table, body.encode("utf-8"),
                   content_type="application/x-ndjson")
        return len(rows)

    def query(self, sql):
        """Run ``sql`` and return its rows as dicts.

        The caller writes the ``FORMAT`` clause it wants, or gets ``JSON`` by
        default here so a bare SELECT does not come back as TabSeparated with no
        column names.
        """
        statement = sql if "FORMAT" in sql.upper() else sql.rstrip().rstrip(";") + " FORMAT JSON"
        raw = self._post(statement, b"")
        if not raw.strip():
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClickHouseError("ClickHouse returned a body that is not JSON: %r" % raw[:200]) from exc
        if isinstance(payload, dict):
            return payload.get("data") or []
        return payload

    def ping(self):
        """Cheapest possible read: proves DNS, TLS, credentials and the database."""
        return self.query("SELECT 1")

    def _post(self, statement, body, content_type="text/plain; charset=utf-8"):
        params = urllib.parse.urlencode({"database": self.database, "query": statement})
        request = urllib.request.Request(
            "%s/?%s" % (self.url, params),
            data=body,
            method="POST",
            headers=self._headers(content_type),
        )
        try:
            with http_open(request, self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise ClickHouseError(
                "ClickHouse rejected the statement (HTTP %s): %s" % (exc.code, detail[:1000])
            ) from exc
        except urllib.error.URLError as exc:
            raise ClickHouseError("ClickHouse is unreachable at %s: %s" % (self.url, exc.reason)) from exc

    def _headers(self, content_type):
        headers = {"Content-Type": content_type, "User-Agent": USER_AGENT}
        if self.username:
            headers["X-ClickHouse-User"] = self.username
        if self.password:
            headers["X-ClickHouse-Key"] = self.password
        if self.access_client_id:
            # Cloudflare Access, for an endpoint behind a tunnel. Sent on every
            # request, like the database credentials beside them.
            headers["CF-Access-Client-Id"] = self.access_client_id
            headers["CF-Access-Client-Secret"] = self.access_client_secret
        return headers


def _encode(row):
    """One row as JSON, with the two Python types JSON does not have."""
    return json.dumps(row, separators=(",", ":"), default=_json_default)


def _json_default(value):
    """Serialize what a mapping produces but ``json`` cannot.

    ``Decimal`` becomes a *quoted string*, and that is deliberate: ClickHouse
    parses the digits straight into ``Decimal(18, 2)``, so the value that lands is
    the value the mapping computed. A bare JSON number also works for two-decimal
    money, but only because those values happen to survive a float round trip;
    the string does not depend on that.

    ``datetime`` becomes the ISO 8601 form ``DateTime64`` parses. Everything else
    is a bug in the mapping rather than something to coerce silently.
    """
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise TypeError("cannot send a %s to ClickHouse as JSON" % type(value).__name__)
