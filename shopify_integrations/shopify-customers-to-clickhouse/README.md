# shopify-customers-to-clickhouse

Reads every Shopify customer changed since the last run and upserts it into
**Castor's own ClickHouse table**, `shopify_customers`. One customer becomes one
row.

It is the ingestion half of the Shopify → ClickHouse pipeline: Shopify and
ClickHouse credentials in, rows in the table Castor already reads. Scheduling,
retries, timeouts, crash recovery, durable checkpoints, logs and run history are
Otter's, not this integration's.

**It creates no schema.** The destination table is Castor's and already exists,
so there is no DDL here to apply and none to keep in step. What this job owns is
the *mapping* — [mapping.py](mapping.py) is the file to edit when a column
changes.

## Before you start

### 1. A Shopify app with `read_customers`

The Shopify Dev Dashboard → your app → Settings gives a Client ID and Client
secret. This integration uses the client credentials grant: it exchanges them
for a 24-hour access token on every run. There is no long-lived token to paste
anywhere and nothing appears in the Shopify admin.

The app needs `read_customers`. Nothing here writes to Shopify.

### 2. ClickHouse credentials that can INSERT

The user needs `INSERT` and `SELECT` on `shopify_customers`. It does not need DDL.

The endpoint is the one Castor itself uses: a Cloudflare Tunnel in front of the
prototype ClickHouse, guarded by **Cloudflare Access**. That means two things
beyond a username and password — a service token, and a `User-Agent` that isn't
the Python default (see *Notes* below).

### 3. The three Castor ids

Every row leads with `workspace_id`, `connection_id` and `dataset_id`. They come
from Castor's own control database, and one job instance fills one dataset, so
they are configuration rather than per-record data:

| Setting | What it names |
| --- | --- |
| `CASTOR_WORKSPACE_ID` | the workspace the connection lives in |
| `CASTOR_CONNECTION_ID` | the Shopify connection |
| `CASTOR_DATASET_ID` | the dataset these rows belong to |

**A wrong value does not fail.** These lead the sort key of the table, so a
valid UUID naming the wrong dataset files every customer under it silently. The
manifest pins the three values read from Castor's control database for the
`customers` dataset — the workspace and connection are the same ones the orders
job uses, because both read the same store over the same Shopify connection, but
`CASTOR_DATASET_ID` is `f6651490-b524-4f7f-ba9f-855f692d4c08`, the `customers`
dataset. Check them with `otter inspect` before the first real run.

## Configuration

### Secrets (the daemon's environment)

Copy the project's template and fill it in:

```bash
cd ../..                      # the otter_examples workspace root
cp otter.env.example otter.env && chmod 600 otter.env
```

Six names, and the manifest lists exactly these under `secrets:` — Otter reads
them from the daemon's environment and **refuses to start Python if any is
missing**, so a typo fails immediately rather than halfway through a sync.

| Name | What it is |
| --- | --- |
| `SHOPIFY_CLIENT_ID` | Shopify app client id |
| `SHOPIFY_CLIENT_SECRET` | Shopify app client secret |
| `CLICKHOUSE_USER` | The user the integration inserts as |
| `CLICKHOUSE_PASSWORD` | That user's password |
| `CLICKHOUSE_ACCESS_CLIENT_ID` | Cloudflare Access service token id |
| `CLICKHOUSE_ACCESS_CLIENT_SECRET` | Cloudflare Access service token secret |

The two Access values are the **service token**, not the tunnel token. The tunnel
token authenticates `cloudflared` to Cloudflare so the tunnel exists; the service
token authenticates a *client* crossing it. They live in different places.

### Settings (`otter.yaml`, `env:`)

Non-secret and deployment-specific. These are printed in full by `otter inspect`,
which is why no credential belongs here.

| Name | Default | What it is |
| --- | --- | --- |
| `SHOPIFY_STORE` | *required* | The `*.myshopify.com` domain |
| `SHOPIFY_API_VERSION` | `2026-07` | Pinned deliberately; bump it on purpose |
| `CLICKHOUSE_URL` | *required* | Scheme, host and port only, e.g. `https://clickhouse-dev.castorhq.com` |
| `CLICKHOUSE_DATABASE` | `castor` | |
| `CLICKHOUSE_CUSTOMERS_TABLE` | `shopify_customers` | Must be a bare identifier |
| `CASTOR_WORKSPACE_ID` | *required* | UUID; see above |
| `CASTOR_CONNECTION_ID` | *required* | UUID; see above |
| `CASTOR_DATASET_ID` | *required* | UUID; see above |
| `BACKFILL_FROM` | `2026-01-01T00:00:00Z` | Where the very first run starts |
| `BACKFILL_DAYS` | `30` | How far back a first run looks when `BACKFILL_FROM` is unset |
| `PAGE_SIZE` | `100` | Customers per GraphQL page |
| `MAX_PAGES_PER_RUN` | `20` | Pages one run will drain before stopping |
| `RUN_BUDGET_SECONDS` | `240` | Stop early and checkpoint rather than be killed at the manifest timeout |
| `OVERLAP_SECONDS` | `600` | How far each window re-scans, to close the race with a record changed mid-run |
| `DRY_RUN` | `0` | `1` rehearses: reads Shopify, writes nothing, advances no watermark |

## Running it

```bash
cd shopify_integrations/shopify-customers-to-clickhouse

# 1. Check the manifest. Local, no daemon, and it catches a bad python.path, a
#    malformed env block or a non-UUID tenant id before anything else.
otter validate .

# 2. Prepare the managed Python environment, snapshot the integration and its
#    shared code into an immutable release, and activate it.
otter release .

# 3. A read-only rehearsal: reads a page from Shopify and logs the rows it would
#    insert, without writing to ClickHouse or moving the watermark.
DRY_RUN=1 otter run .
```

Then either run it on demand or give it a cadence:

```bash
otter run .
otter schedule set '*/15 * * * *'     # or PUT /v1/jobs/shopify-customers-to-clickhouse/schedule
otter schedule show
```

`otter.yaml` declares `*/5 * * * *`, which Otter imports **once**, when it first
sees the job. After that the schedule is runtime state: edit it with
`otter schedule set` or the API, and changing the manifest has no effect.

## Verifying it worked

The destination is the assertion, not the exit code.

```sql
-- How many customers landed, for this dataset.
SELECT count() FROM castor.shopify_customers FINAL
WHERE workspace_id = '<workspace>' AND dataset_id = '<dataset>';

-- Duplicate keys: zero once merges have caught up. A growing number means the
-- same customer is being written with different values -- a mapping bug, not a
-- merge backlog.
SELECT count() - uniqExact(shopify_customer_id)
FROM castor.shopify_customers FINAL
WHERE dataset_id = '<dataset>';
```

What the integration records about itself is in `ctx.state`, readable with:

```bash
otter state get shopify-customers-to-clickhouse last_run
```

It holds the page count, how many customers were fetched and written, whether
the window drained, and when it started and finished.

## How it decides what to read

**The window is `updated_at`, and nothing else.** The filter is
`updated_at:>'<window start>'`, which is what makes a run incremental rather
than a rescan of the whole customer list. There is deliberately **no
`status:any`**. The orders job needs that clause because Shopify's `orders`
connection returns **open** orders only by default, so without it closed,
cancelled and archived orders never reach the destination. The `customers`
connection has no such default — that was checked against the live API rather
than assumed: `customersCount` with no filter equals the count returned by an
unfiltered walk, so nothing is hidden from this query. Copying the clause here
would be cargo cult, and a value Shopify does not recognise would narrow the
result set rather than error.

**The filter's field name is not validated by Shopify.** An unknown field in a
search string matches everything rather than erroring, which would quietly turn
an incremental sync into a full rescan every five minutes. That was verified
against the live API too: a deliberately bogus field returned every record. A bad
*value* fails the other way, by narrowing. Neither is an error, so
`tests/test_source.py` asserts the exact string rather than trusting Shopify to
complain.

**Each window overlaps the last by `OVERLAP_SECONDS`.** A record changed while a
run was executing is picked up by the next one rather than being missed by a
strictly-increasing cursor. The overlap costs nothing, because every write is an
upsert.

**A run checkpoints after each page, never before it.** The cursor is saved once
the rows have reached ClickHouse, so a crash resumes at the next page rather than
skipping one it never wrote. A window that drains commits the watermark; a window
that stops early keeps the cursor and resumes on the next tick.

**A run stops at its budget rather than being killed.** `RUN_BUDGET_SECONDS` is
below the manifest's `timeout`, so the loop exits cleanly with a resumable
checkpoint instead of being `SIGTERM`ed mid-page.

**An insert failure fails the run.** ClickHouse's error names the column and the
value that broke it; the useful response is to stop and read it rather than carry
on with the next page. The cursor has not advanced, so the retry re-does that
page, and the upsert makes that free.

## Notes

**There was no importer to transcribe, and that is the one thing this mapping
does differently from its siblings.** [mapping.py](mapping.py) is usually a
careful transcription of Castor's import worker
(`castor-app/infra/lambda/import-worker/handler.py`), because the Lambda and the
Otter job write the same table and the two must not disagree about a value. That
worker never implemented customers: it served the other resources the
application exposed — products and orders among them — and it never offered
customers as a resource at all. So
this mapping was written against the live Admin API's `Customer` type instead,
with the customer fields the worker *did* carry on an order followed for
consistency. Nullability comes from the schema rather than from what a sample
happened to return: `firstName`, `lastName`, `email` and `phone` are nullable
there and the destination columns are nullable to match.

**Why `defaultEmailAddress` and `defaultPhoneNumber` rather than `email` and
`phone`.** `Customer.email` and `Customer.phone` are still accepted by the pinned
API version and still return values, but they no longer appear in the type's
introspection at all — which is what a removed field looks like before it stops
answering. The replacements are what the orders job already reads for its
customer block, so the two jobs agree about which address belongs to a customer,
and [queries/customers.graphql](queries/customers.graphql) selects the nested
fields because those are the ones that will still be there. No money column is
carried for the same kind of reason: customers have no prices.

**Why the client sends its own `User-Agent`.** Cloudflare's browser-integrity
check rejects the default `Python-urllib/x.y` signature with a 403 whose body is
`error code: 1010` — *before* Access evaluates the service token and before
ClickHouse sees the request. The failure names neither the token nor the
database, so it reads like a credentials problem when the credentials are fine.
`otter_connectors.clickhouse` identifies itself instead.

**Why this targets Castor's schema rather than defining its own.** The point of
the work is that these rows land where Castor reads them, and the destination's
sort key is `(workspace_id, dataset_id, <shopify id>)` — which is what lets two
datasets hold the same Shopify customer without collapsing into one row. A
standalone table keyed on the Shopify id alone would be wrong for that, so the
job writes to the real one.

## Layout

| File | What it decides |
| --- | --- |
| `main.py` | Wiring only: build the clients, pick the sink, drain, report |
| `settings.py` | Every knob, read and validated in one place |
| `source.py` | What to read from Shopify, and the `.graphql` document |
| `mapping.py` | **Where a Shopify field becomes a ClickHouse column** |
| `clickhouse_sync.py` | The page loop and its two sinks |
| `sync_window.py` | The resumable window and what a dry run persists |
| `run_state.py` | What the run logs, and what it writes to `ctx.state` |
| `queries/customers.graphql` | The GraphQL document, as a first-class file |

The Shopify and ClickHouse clients and the resumable watermark live in
`../lib/python/otter_connectors`, shared with the other integrations. The
ClickHouse client is `urllib` against ClickHouse's HTTP interface, which is why
this project has no dependencies and `uv.lock` stays trivial.

## Tests

```bash
python3 -m unittest discover -s tests
```

No store, no database and no network: the Shopify client, the sink and the clock
are all fakes, so the budget stop, the page cap, the checkpoint ordering, a
non-advancing cursor, a rejected insert and the tenant ids are all reachable in
milliseconds.
