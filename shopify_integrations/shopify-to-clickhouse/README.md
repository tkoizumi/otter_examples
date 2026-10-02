# shopify-to-clickhouse

Reads every Shopify product changed since the last run, with its variants, and
upserts them into **Castor's own ClickHouse tables**, `shopify_products` and
`shopify_product_variants`. One product becomes one row, one variant becomes one
row.

It is the ingestion half of the Shopify → ClickHouse pipeline: Shopify and
ClickHouse credentials in, rows in the tables Castor already reads. Scheduling,
retries, timeouts, crash recovery, durable checkpoints, logs and run history are
Otter's, not this integration's.

**It creates no schema.** The destination tables are Castor's and already exist,
so there is no DDL here to apply and none to keep in step. What this job owns is
the *mapping* — [mapping.py](mapping.py) is the file to edit when a column
changes.

## Before you start

### 1. A Shopify app with `read_products`

The Shopify Dev Dashboard → your app → Settings gives a Client ID and Client
secret. This integration uses the client credentials grant: it exchanges them for
a 24-hour access token on every run. There is no long-lived token to paste
anywhere and nothing appears in the Shopify admin.

The app needs `read_products`. Nothing here writes to Shopify.

### 2. ClickHouse credentials that can INSERT

The user needs `INSERT` and `SELECT` on the two tables. It does not need DDL.

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

**A wrong value does not fail.** These lead the sort key of both tables, so a
valid UUID naming the wrong dataset files every product under it silently. The
manifest ships zeros as an obvious placeholder; replace them before the first
real run.

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
| `CLICKHOUSE_PRODUCTS_TABLE` | `shopify_products` | Must be a bare identifier |
| `CLICKHOUSE_VARIANTS_TABLE` | `shopify_product_variants` | Must be a bare identifier |
| `CASTOR_WORKSPACE_ID` | *required* | UUID; see above |
| `CASTOR_CONNECTION_ID` | *required* | UUID; see above |
| `CASTOR_DATASET_ID` | *required* | UUID; see above |
| `BACKFILL_FROM` | `2026-01-01T00:00:00Z` | Where the very first run starts |
| `BACKFILL_DAYS` | `30` | How far back a first run looks when `BACKFILL_FROM` is unset |
| `PAGE_SIZE` | `100` | Products per GraphQL page |
| `MAX_PAGES_PER_RUN` | `20` | Pages one run will drain before stopping |
| `RUN_BUDGET_SECONDS` | `240` | Stop early and checkpoint rather than be killed at the manifest timeout |
| `OVERLAP_SECONDS` | `600` | How far each window re-scans, to close the race with a record changed mid-run |
| `DRY_RUN` | `0` | `1` rehearses: reads Shopify, writes nothing, advances no watermark |

## Running it

```bash
cd shopify_integrations/shopify-to-clickhouse

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
otter schedule set '*/15 * * * *'     # or PUT /v1/jobs/shopify-to-clickhouse/schedule
otter schedule show
```

`otter.yaml` declares `*/5 * * * *`, which Otter imports **once**, when it first
sees the job. After that the schedule is runtime state: edit it with
`otter schedule set` or the API, and changing the manifest has no effect.

## Verifying it worked

The destination is the assertion, not the exit code.

```sql
-- How many objects landed, for this dataset.
SELECT count() FROM castor.shopify_products FINAL
WHERE workspace_id = '<workspace>' AND dataset_id = '<dataset>';

-- Duplicate keys: zero once merges have caught up. A growing number means the
-- same object is being written with different values -- a mapping bug, not a
-- merge backlog.
SELECT count() - uniqExact(shopify_product_id)
FROM castor.shopify_products FINAL
WHERE dataset_id = '<dataset>';

-- A variant joined to its product, which is the read this exists to enable.
SELECT p.title, v.sku, v.price
FROM castor.shopify_product_variants AS v FINAL
INNER JOIN castor.shopify_products AS p FINAL
    ON p.workspace_id = v.workspace_id
   AND p.dataset_id = v.dataset_id
   AND p.shopify_product_id = v.shopify_product_id
LIMIT 10;
```

What the integration records about itself is in `ctx.state`, readable with:

```bash
otter state get shopify-to-clickhouse last_run
```

It holds the page count, how many products and variants were fetched and written,
whether the window drained, and when it started and finished.

## How it decides what to read

**The watermark is on the product, not the variant.** The root connection is
`products`, and the window is `updated_at:>'<window start>'`. A product-level
change does not bump its variants' `updatedAt`, so a variant-rooted watermark
would never re-sync a renamed product — and the product row carries the fields
most likely to change.

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

**Variants past the nested page are fetched explicitly.** Shopify caps the nested
`variants(first: 100)` connection inside a product. `pageInfo` is selected, and
the remainder is fetched with `product-variants.graphql` — a product with more
variants than one nested page is detected rather than silently truncated.

**An insert failure fails the run.** ClickHouse's error names the column and the
value that broke it; the useful response is to stop and read it rather than carry
on with the next page. The cursor has not advanced, so the retry re-does that
page, and the upsert makes that free.

## Notes

**Why the client sends its own `User-Agent`.** Cloudflare's browser-integrity
check rejects the default `Python-urllib/x.y` signature with a 403 whose body is
`error code: 1010` — *before* Access evaluates the service token and before
ClickHouse sees the request. The failure names neither the token nor the
database, so it reads like a credentials problem when the credentials are fine.
`otter_connectors.clickhouse` identifies itself instead.

**Why this targets Castor's schema rather than defining its own.** The point of
the work is that these rows land where Castor reads them, and the destination's
sort key is `(workspace_id, dataset_id, <shopify id>)` — which is what lets two
datasets hold the same Shopify product without collapsing into one row. A
standalone table keyed on the Shopify id alone would be wrong for that, so the
job writes to the real one.

## Layout

| File | What it decides |
| --- | --- |
| `main.py` | Wiring only: build the clients, pick the sink, drain, report |
| `settings.py` | Every knob, read and validated in one place |
| `source.py` | What to read from Shopify, and the `.graphql` documents |
| `mapping.py` | **Where a Shopify field becomes a ClickHouse column** |
| `clickhouse_sync.py` | The page loop and its two sinks |
| `sync_window.py` | The resumable window and what a dry run persists |
| `run_state.py` | What the run logs, and what it writes to `ctx.state` |
| `queries/*.graphql` | The GraphQL documents, as first-class files |

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
