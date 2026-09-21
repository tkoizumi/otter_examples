# shopify-product-to-salesforce-product

Upserts Shopify product variants into Salesforce Product2 every five minutes,
incrementally and idempotently.

```text
Shopify Admin API (GraphQL)          Otter                      Salesforce REST API
  products(updated_at:>watermark)  ──►  child Python process  ──►  composite/sobjects upsert
    └── variants (nested, paged)          state: watermark,         by Shopify_Variant_Id__c
  cursor pagination                           page cursor,
                                              dead letters
```

Runtime primitives (scheduling, the watermark, retries, timeouts, logs, run
history) are all Otter's. The vendor clients are not — they live in a shared
library, so this file holds only what is specific to this sync.

> **The integration is addressed by the manifest's `name:`.** It is
> `shopify-product-to-salesforce-product`, which matches this directory, and
> that is the string every CLI command takes. From inside this directory,
> `otter validate .` does the same without naming it.

## One Product2 per variant

The unit of sync is the **variant**, not the product. Shopify's `productVariants`
connection is the flatter one and was the obvious root, but a product-level
change does not bump its variants' `updatedAt` — in the store this was built
against, 14 of 17 products are newer than every one of their variants. A
variant-level watermark therefore misses a renamed product forever, and the
mapping reads the product title. So the root is `products` and the watermark is
compared against `Product.updatedAt`.

Each variant still becomes its own Salesforce record, keyed on
`Shopify_Variant_Id__c`, and each row carries its parent's id in
`Shopify_Product_Id__c` so the rows remain attributable to one product. A
Shopify product with three variants is three Product2 rows.

**The mapping is written against a variant, though.** `source.variants_with_product`
hands each variant its parent product under `product`, so
`ProductVariant.product.id` names the product the variant was fetched beneath and
`mapping.py` stays the only place a field is named.

## Where this sits in the workspace

One integration directory inside the `shopify_integrations` grouping directory.
The Otter project root is the repository root, which is where `otter.env` and
`.otter/` live. Paths below are relative to this directory unless stated
otherwise.

```text
otter_examples/                                     the Otter project root
├── otter.env                                       secrets; 0600, gitignored, loaded by `otter start`
├── otter.env.example                               the committed template to copy
├── .gitignore                                      keeps every env file out of git
└── shopify_integrations/                           a grouping directory, not a project boundary
    ├── lib/python/                                 shared code, snapshotted into each release
    │   ├── otter_connectors/
    │   │   ├── shopify.py        Shopify GraphQL client: client credentials grant,
    │   │   │                     throttle backoff, Relay cursor paging
    │   │   ├── salesforce.py     Salesforce REST client: OAuth, batched upsert by
    │   │   │                     External ID, picklist-aware values
    │   │   ├── records.py        record building: dotted paths, drop empties,
    │   │   │                     truncate, mapping validation
    │   │   ├── checkpoint.py     Watermark: resumable "what have I processed?" state
    │   │   ├── config.py         env / env_int / env_bool / require_env
    │   │   ├── http.py           the CA-aware opener both clients share
    │   │   ├── timeutil.py       utcnow / to_iso / parse_iso
    │   │   └── errors.py         ConnectorError, ConfigError
    │   ├── otter_schema/         schema references, and the puller that writes them
    │   └── tests/
    ├── shopify_customer_to_salesforce_contact/     the other integration
    └── shopify-product-to-salesforce-product/      <- you are here
        ├── otter.yaml        when and how it runs
        ├── source.py         what we read from Shopify: the queries and their paging
        ├── mapping.py        where it lands: the Product2 mapping
        ├── settings.py       every knob this sync reads, in one place
        ├── product_sync.py   the page loop, and the sink a dry run uses
        ├── sync_window.py    the resumable window and its checkpointing
        ├── run_state.py      what each run records about itself
        ├── queries/          products.graphql, product-variants.graphql
        ├── schema/           pulled Shopify + Salesforce schema references
        └── tests/            test_main.py, test_settings.py, test_source.py
```

**Editing this integration usually means editing `mapping.py` and the variant
selections in `queries/`.** The mapping names fields through `schema/` references
rather than strings, so a wrong name fails at import; the queries are real
`.graphql` documents, so a field that does not exist fails a test rather than
arriving as an empty value in a run that still reports `succeeded`.

`mapping.py` is deliberately pure — no environment reads, no I/O, nothing
imported from `main` or `source` — so it is testable on a plain dict and could be
lifted into a shared package unchanged.

Nothing special makes the sibling imports work: Otter runs `python3 main.py`
with the working directory set to the integration directory, so Python puts that
directory on `sys.path` itself.

The manifest points at the shared library, which Otter puts on the child's
`PYTHONPATH` — no install step, and `otter validate` fails if the path is wrong:

```yaml
python:
  mode: managed          # Otter prepares the interpreter named in .python-version
  path:
    - ../lib/python
```

See [`lib/python/README.md`](../lib/python/README.md) for why this is not part of
the Otter SDK, and how to reuse it from another integration.

- [Before you start](#before-you-start)
- [Secrets: where they live](#secrets-where-they-live)
- [Configuration](#configuration)
- [Running it](#running-it)
- [The schedule](#the-schedule)
- [Day two operations](#day-two-operations)
- [Field mapping](#field-mapping)
- [Tests](#tests)
- [How it behaves when things go wrong](#how-it-behaves-when-things-go-wrong)
- [Gotchas](#gotchas)
- [Later improvements](#later-improvements)

---

## Before you start

Three things must exist before the first run. None of them are things Otter can
do for you.

### 1. A Shopify app with `read_products`

This integration uses the GraphQL Admin API for the same reason the customer
sync does: the Admin REST API is a legacy API, and admin-created custom apps can
no longer be created. A server-side integration acting on your own stores uses
the [client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant),
exchanging its own client ID and secret for a 24 hour access token that it renews
on every run. There is no long-lived Shopify token anywhere.

1. Create an app in the **[Dev Dashboard](https://dev.shopify.com/dashboard/)**
   (not the Shopify admin's *Develop apps* page).
2. On the app's **version**, select the `read_products` scope and release it.
   Scopes live on the version, so adding one later means releasing a new version.
   Products and their variants are covered by this one scope; unlike customer
   data, they are **not** protected customer data, so there is no approval step.
3. **Install the app on your store.**
4. Copy the **Client ID** and **Client secret** from the app's **Settings**.

As with the customer sync, the grant only works when the app and the store
belong to the **same Shopify organization**. Otherwise every token request fails
with:

```text
Oauth error shop_not_permitted: Client credentials cannot be performed on this shop.
```

Then confirm the scope and the query work before wiring anything up:

```bash
export SHOPIFY_STORE=your-store.myshopify.com
export SHOPIFY_CLIENT_ID=...
export SHOPIFY_CLIENT_SECRET=...

# 1. Exchange credentials for a 24 hour token
TOKEN=$(curl -sS -X POST "https://$SHOPIFY_STORE/admin/oauth/access_token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d grant_type=client_credentials \
  -d client_id="$SHOPIFY_CLIENT_ID" \
  -d client_secret="$SHOPIFY_CLIENT_SECRET" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("access_token") or d)')
echo "$TOKEN"

# 2. Use it: a page of products with their first few variants
curl -sS -X POST "https://$SHOPIFY_STORE/admin/api/2026-07/graphql.json" \
  -H "X-Shopify-Access-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  --data @- <<'JSON'
{"query":"query { products(first: 2, sortKey: UPDATED_AT) { nodes { id title updatedAt hasOnlyDefaultVariant variants(first: 3) { nodes { id title sku } } } } }"}
JSON
```

You should see real product and variant titles. An authorization error here means
the `read_products` scope is missing from the released app version.

### 2. Two custom fields on Salesforce Product2

The upsert keys on a custom field that must be marked **External ID** and
**Unique**, and the parent product id needs a plain text field beside it:

1. Setup → Object Manager → **Product2** → Fields & Relationships → **New**
2. Type **Text**, length **200**
3. Field Label `Shopify Variant Id`, Field Name `Shopify_Variant_Id__c`
4. Tick **External ID** and **Unique**, then Save
5. Repeat for `Shopify Product Id` / `Shopify_Product_Id__c`, length **200**, with
   neither box ticked

If `Shopify_Variant_Id__c` is not marked External ID, every write fails with
`INVALID_FIELD`. Rename the fields and update `SALESFORCE_EXTERNAL_ID_FIELD` in
`otter.yaml` plus `mapping.py` if you prefer different names.

`Name` is standard, required on Product2, and the only other field written today.
The committed `schema/salesforce/product2.py` is a snapshot pulled from the org
this example was built against; it is what `mapping.py` validates its names
against, **not your org**. If your fields differ, either create them to match or
change the mapping and re-pull the schema (see [Tests](#tests) and
[`otter_schema/README.md`](../lib/python/otter_schema/README.md)).

### 3. Salesforce OAuth credentials

This integration defaults to the **client credentials** flow, which is the right
choice for an unattended server: no password, no security token, nothing to
rotate monthly.

1. Setup → App Manager → **New Connected App**
2. Enable OAuth Settings. Any callback URL will do
   (`https://login.salesforce.com/services/oauth2/success`).
3. OAuth scope: **Manage user data via APIs (`api`)**
4. Tick **Enable Client Credentials Flow**, then set the **Run As** user to a
   dedicated integration user that can read and write Products.
5. Save, wait a few minutes for it to propagate, then copy the **Consumer Key**
   and **Consumer Secret**.
6. Make sure **My Domain** is deployed — the token endpoint is your My Domain
   URL, not `login.salesforce.com`.

Prefer the **username-password** flow instead? Set `SALESFORCE_AUTH: password`
and provide `SALESFORCE_USERNAME` plus `SALESFORCE_PASSWORD` (the password with
the security token appended). It works, but it needs the connected app's
"Allow OAuth Username-Password Flows" enabled and it breaks whenever the user
changes their password or the org's IP restrictions change.

---

## Secrets: where they live

`otter.yaml` lists four **names** under `secrets:` and holds **no values**:

```yaml
secrets:
  - SHOPIFY_CLIENT_ID
  - SHOPIFY_CLIENT_SECRET
  - SALESFORCE_CLIENT_ID
  - SALESFORCE_CLIENT_SECRET
```

They are the same four names the customer sync declares, because one daemon
environment serves every integration in the project.

Otter reads those names from the **daemon's environment**, injects them into the
child process, and refuses to start Python at all if one is missing. The values
belong in one file, at the project root:

```text
otter.env        # repository root, next to .otter/, mode 0600, gitignored
```

Copy the committed template once and fill it in — from the repository root:

```bash
cp otter.env.example otter.env
chmod 600 otter.env
$EDITOR otter.env
```

Then — and this is the step that trips everyone up:

> **Secrets are read when the daemon starts, not when a run starts.**
> Editing `otter.env` under a running daemon changes nothing. Restart it:
>
> ```bash
> otter stop && otter start --detach
> ```

A run against a daemon that predates the file fails in about a millisecond:

```text
not started: integration shopify-product-to-salesforce-product requires
secrets that are not available: SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET,
SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
```

If you see that, check in this order:

1. The daemon was started **after** `otter.env` was written (`otter status`
   shows uptime — compare it against the file's mtime).
2. The keys are spelled exactly as they are in `secrets:`.
3. The shell that ran `otter start` did not already export any of the four. A
   variable already set in the environment **wins over the file**, and an empty
   export is still "set".
4. You used `otter start`. `otter serve` and bare `otterd` do **not** load
   `otter.env` — they take only the environment they inherit.

Things that are true once it is set up:

- **`otter inspect` never prints secrets.** It prints the manifest's whole
  `env:` block, which is why no value may ever be written into `otter.yaml`.
- **The daemon's full environment reaches the child**, not just declared
  secrets, so the operational knobs below can be set in `otter.env`. A key the
  manifest also defines under `env:` is overridden by the manifest, and a
  manifest value may pull from the daemon environment with `${VAR}`:

  ```yaml
  env:
    SHOPIFY_STORE: ${PROD_SHOPIFY_STORE}
  ```

- **Deploying to a host:** `otter deploy` reads `otter.env` (flag `--env-file`)
  and installs it on the remote as the shared credentials file, which the
  systemd unit loads as `EnvironmentFile=-/etc/otter/shared.env`. One file for
  every integration, because the daemon's environment is a single process
  environment.

---

## Configuration

### In `otter.yaml` (non-secret; edits go here)

These are the values currently in the manifest — replace them with your own.

| Key | Current value | Notes |
| --- | --- | --- |
| `SHOPIFY_STORE` | `robin-dev-3.myshopify.com` | Your `*.myshopify.com` domain. |
| `SHOPIFY_API_VERSION` | `2026-07` | Pin it; bump deliberately. |
| `SALESFORCE_INSTANCE_URL` | `https://drive-energy-1561.my.salesforce.com` | My Domain; also the token endpoint. |
| `SALESFORCE_API_VERSION` | `62.0` | Any version your org supports. |
| `SALESFORCE_AUTH` | `client_credentials` | Or `password`. |
| `SALESFORCE_OBJECT` | `Product2` | Where variants land. |
| `SALESFORCE_EXTERNAL_ID_FIELD` | `Shopify_Variant_Id__c` | Must be External ID + Unique. |
| `BACKFILL_FROM` | `2026-01-01T00:00:00Z` | Where the *first ever* run starts. |

### In the daemon's environment (operational knobs, set per deployment)

These are read with sensible defaults and are deliberately **not** in the
manifest, so exporting them for the daemon (or putting them in `otter.env`)
overrides them without editing a committed file. Because they arrive through the
daemon's environment, changing one means a restart, exactly like a secret.

| Key | Default | Purpose |
| --- | --- | --- |
| `PAGE_SIZE` | `100` | Products per Shopify page. |
| `MAX_PAGES_PER_RUN` | `20` | Upper bound on work per run (20 × 100 = 2000 products). One page can yield many variant records, so this bounds products, not rows. |
| `RUN_BUDGET_SECONDS` | `240` | Wall-clock budget, kept under the 300s timeout. |
| `OVERLAP_SECONDS` | `600` | How far back each window re-scans. |
| `BACKFILL_DAYS` | `30` | Used only when `BACKFILL_FROM` is unset. |
| `SALESFORCE_BATCH_SIZE` | `200` | Records per composite call; `1` forces per-record writes. |
| `DRY_RUN` | `0` | Set `1` to read Shopify and log writes without touching Salesforce. |
| `SHOPIFY_API_BASE` | derived from store + version | Override to point at a mock. |
| `SHOPIFY_TOKEN_URL` | `https://<store>/admin/oauth/access_token` | Override to point at a mock. |
| `SALESFORCE_USERNAME` / `SALESFORCE_PASSWORD` | unset | Password flow only. |

`SALESFORCE_BATCH_SIZE`, `SHOPIFY_API_BASE` and `SHOPIFY_TOKEN_URL` configure the
clients rather than this sync, so they are read by `otter_connectors.clients` and
not by `settings.py`. Nothing else in the run uses them.

### Secrets (daemon environment, listed under `secrets:`)

`SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, `SALESFORCE_CLIENT_ID`,
`SALESFORCE_CLIENT_SECRET`.

`SHOPIFY_ACCESS_TOKEN` is read from the environment but deliberately not listed,
so it stays optional: set it only if you hold a pre-generated token from a
legacy admin-created custom app, and it is then used verbatim instead of the
grant.

`SALESFORCE_USERNAME` and `SALESFORCE_PASSWORD` are likewise optional, for the
password flow. If you use either pair, add the keys to `secrets:` too — then a
missing value fails the run immediately with a clear message instead of part
way through.

### Why there is no Shopify token in this file

Tokens from the client credentials grant last 24 hours, and Otter runs each
attempt in a **fresh process**, so an in-memory cache would never survive a run
anyway. The integration therefore requests a token at the start of every run,
and refreshes once mid-run if Shopify answers `401`/`403` — which covers a
secret rotation or a token lapsing between pages.

---

## Running it

All commands run from the project root (the repository root) unless noted. The
integration is addressed by its manifest name.

```bash
# 1. Check the manifest. Runs locally, needs no daemon, and catches a bad
#    python.path or a malformed env block before anything else. The argument is
#    the manifest name; a path (shopify_integrations/<dir>) works too.
otter validate shopify-product-to-salesforce-product

# 2. Snapshot the integration and its shared code into an immutable release and
#    activate it. Runs execute the ACTIVE RELEASE, so an edit to main.py,
#    source.py or mapping.py is not live until this runs again.
otter release shopify-product-to-salesforce-product

# 3. Start the daemon. This is the step that loads otter.env.
otter start --detach
otter status
```

`otter run` refuses with `has no active release` (HTTP 409) if you skip the
release step.

**Dry run first.** `DRY_RUN` is read from the daemon's environment, so put it in
`otter.env` (or export it) and restart:

```bash
DRY_RUN=1 otter start --detach       # no writes, watermark not advanced
otter run shopify-product-to-salesforce-product
otter logs <run-id> | head -40
```

Look for `dry run: would upsert` lines containing real product names, then
confirm nothing was recorded:

```bash
otter state get shopify-product-to-salesforce-product sync_cursor
# otter: shopify-product-to-salesforce-product/sync_cursor is not set
```

A dry run persists **nothing**, including the in-window page cursor, so a
multi-page rehearsal cannot make the next real run resume past records it never
wrote.

**Then for real.** Restart without `DRY_RUN` and run again. The first run
backfills from `BACKFILL_FROM`, `MAX_PAGES_PER_RUN` pages at a time. If you have
more products than fit in one run it exits **succeeded** with `complete: false`,
and the next run continues from the saved page cursor — no data is lost and
nothing is written twice. Watch progress:

```bash
otter state get shopify-product-to-salesforce-product last_run
otter state get shopify-product-to-salesforce-product in_progress_cursor
```

Once a window drains, the watermark advances and later runs only pick up
products changed since. Confirm with a second run — it should fetch nothing:

```bash
otter logs "$(otter run shopify-product-to-salesforce-product)" | grep 'sync finished'
# sync finished {"complete":true,"failed":0,"fetched":0,"pages":1,"written":0,...}
```

---

## The schedule

The manifest already has the trigger:

```yaml
trigger:
  cron: "*/5 * * * *"
```

Otter registers cron triggers from the manifest on every start, so there is
nothing else to enable — a daemon running with this integration fires every five
minutes. To work without the schedule first, comment the `trigger:` block out and
restart; manual runs work either way.

The manifest also pins `timeout: 300` and `concurrency: 1`: a single run cannot
overrun its own five-minute schedule, and two runs never race over the same
cursor. `RUN_BUDGET_SECONDS` is deliberately below the timeout so a run stops
early with a resumable checkpoint instead of being killed mid-page and marked
`timed_out`. `retry.attempts` is 3 with a 30s initial delay, so a retry lands
inside the next cron tick.

Deployed, `otter deploy` writes the systemd unit for you. It looks like this:

```ini
[Unit]
Description=Otter integration runtime
After=network-online.target

[Service]
User=otter
EnvironmentFile=-/etc/otter/shared.env
ExecStart=/usr/local/bin/otterd \
  --integrations /srv/otter/integrations \
  --data /var/lib/otter \
  --workers 4 \
  --log-format json
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

The leading `-` on `EnvironmentFile` means the unit still starts if the file is
absent; it will simply fail every run with the "secrets are not available"
message above until you install it.

---

## Day two operations

```bash
otter status                                                       # queue depth, run counts
otter integrations --schedule                                      # cron, next run, last outcome
otter inspect shopify-product-to-salesforce-product                # config, cron, next fire time
otter runs --integration shopify-product-to-salesforce-product --limit 20
otter logs <run-id> --follow
otter state get shopify-product-to-salesforce-product last_run
otter state get shopify-product-to-salesforce-product failed_products
otter state get shopify-product-to-salesforce-product failed_total
```

`last_run` holds `pages`, `fetched`, `written`, `failed`, `complete`,
`started_at`, `finished_at`, `window_start` and `dry_run`. The watermark lives in
three keys, all managed by `otter_connectors.checkpoint.Watermark`:

| Key | Meaning |
| --- | --- |
| `sync_cursor` | The committed watermark: everything up to here is done. |
| `in_progress_window_start` | The lower bound of a window that has not drained yet. |
| `in_progress_cursor` | The Shopify page cursor inside that window. |

**Re-read the dead letters.** Records Salesforce permanently rejected are kept in
`failed_products` (most recent 100) with the exact error; `failed_total` counts
all of them. Fix the cause — usually the External ID field not being flagged as
one, a validation rule, or a required field — then re-sync those variants by
rewinding the watermark:

```bash
otter state set shopify-product-to-salesforce-product sync_cursor '"2026-01-01T00:00:00Z"'
otter state delete shopify-product-to-salesforce-product in_progress_cursor
otter state delete shopify-product-to-salesforce-product in_progress_window_start
otter run shopify-product-to-salesforce-product
```

Rewinding is safe: every write is an upsert, so re-syncing updates the same
Product2 rows rather than duplicating them.

**Force a full re-sync.** Same as above with `BACKFILL_FROM`'s value.

**Pause the integration.** Comment out the `trigger:` block, `otter release` it
again, and restart the daemon; manual runs still work. Queued and running jobs
are unaffected.

**Ship a code change.** Edit `mapping.py` / `queries/*.graphql`, run
`otter validate`, then `otter release` again. Until you do, runs keep executing
the previous release.

---

## Field mapping

`mapping.py` builds one record per variant, and every key is a reference into
the pulled Salesforce schema rather than a string:

| Shopify | Salesforce Product2 |
| --- | --- |
| `product.id` (numeric tail) | `Shopify_Product_Id__c` |
| `id` (numeric tail) | `Shopify_Variant_Id__c` — the upsert key |
| `product.title`, qualified by `title` | `Name` |

`numeric_id` is why the two id entries are `(path, transform)` pairs rather than
bare paths: it turns the GraphQL GID (`gid://shopify/ProductVariant/123`) into
the trailing number the customer sync has always stored.

The name rule is the only real logic. Shopify gives the lone variant of a
single-variant product the title **"Default Title"**, so mapping `Name` straight
from the variant title would call most records "Default Title" — a data bug that
looks like a working sync. `variant_name()` uses `hasOnlyDefaultVariant` to tell
the two cases apart:

| Shopify state | `Name` in Salesforce |
| --- | --- |
| `hasOnlyDefaultVariant: true` | the product title |
| variant title is empty or `"Default Title"` | the product title |
| otherwise | `"<product title> - <variant title>"` |

Target lengths come from the schema references themselves — `build_record` reads
each target's declared length off the mapping key — so there is no truncation
table to keep in step. Empty values are **omitted**, not sent as `null`, so a
sync never wipes a field someone filled in by hand.

To add a field, add an entry to `variant_mapping()`, then make sure it is
selected by **both** variant selections — the nested `variants` in
`queries/products.graphql` and `queries/product-variants.graphql`, which fetches
the overflow past Shopify's nested page cap. `tests/test_source.py` fails if the
mapping reads a path `products.graphql` does not select, but it checks only that
document: a field selected there and missing from `product-variants.graphql`
would arrive empty for any product with more variants than one nested page
holds. Both documents already select `sku` and `price` on every variant, so the
cheapest extensions are:

```python
Product2.ProductCode: ProductVariant.sku,          # length 255
Product2.StockKeepingUnit: ProductVariant.sku,     # length 180
```

Anything beyond that needs a matching custom field in Salesforce first, or every
write fails with `INVALID_FIELD`.

---

## Tests

From the repository root:

```bash
# This integration's tests: the page loop and its state (test_main.py), the
# settings (test_settings.py), and the queries, paging and mapping (test_source.py)
PYTHONPATH=shopify_integrations/lib/python:.otter/data/sdk/python \
    python3 -m unittest discover -s shopify_integrations/shopify-product-to-salesforce-product/tests

# The shared library's own tests
PYTHONPATH=shopify_integrations/lib/python \
    python3 -m unittest discover -s shopify_integrations/lib/python/tests
```

`.otter/data/sdk/python` is where `otter start` unpacks the runtime SDK that
`main.py` imports; without it, `test_main.py` cannot import `main`.

`test_source.py` validates `queries/*.graphql` against the pulled
`schema/shopify/shopify.graphql` with `graphql-core`, which is an **optional** dev
dependency — the tests that need it skip when it is absent (11 skips of 58). To
run them:

```bash
pip install 'graphql-core>=3.2'
# or: pip install -e 'shopify_integrations/lib/python[dev]'
```

The schema snapshot is meant to be committed and re-pulled when it goes stale.
The puller is a plain script, so run it directly:

```bash
python3 shopify_integrations/lib/python/otter_schema/pull.py \
    --integration shopify_integrations/shopify-product-to-salesforce-product \
    --system shopify --object ProductVariant

python3 shopify_integrations/lib/python/otter_schema/pull.py \
    --integration shopify_integrations/shopify-product-to-salesforce-product \
    --object Product2
```

See [`otter_schema/README.md`](../lib/python/otter_schema/README.md) for the flags
and the caveat that matters: a stored schema is a second source of truth, and a
stale one validates confidently against an org that no longer exists.

---

## How it behaves when things go wrong

| Situation | What happens | Why |
| --- | --- | --- |
| Shopify 5xx or unreachable | Retried in-process, then the run fails and Otter retries with backoff | Infrastructure blip; the run is safe to repeat |
| Shopify throttles (GraphQL `THROTTLED`) | Sleeps until the leaky bucket can afford the next query | Avoids burning the retry policy on rate limits |
| Access token lapses or is revoked mid-run | Refreshed once via the client credentials grant, then the page is retried | 24 hour tokens, and a secret rotation invalidates them early |
| Credentials wrong, or store not in the app's organization | Run fails immediately with `shop_not_permitted` or the raw Shopify error | Configuration error; retries cannot help |
| Missing `read_products` scope | GraphQL returns an authorization error for the query | The scope lives on the released app version |
| A product has more variants than the nested page holds | `pageInfo.hasNextPage` is followed with `product-variants.graphql` until the last page | Silently syncing only the first 250 variants is the failure this prevents |
| One variant rejected by Salesforce (`INVALID_FIELD`, validation rule) | Recorded in `failed_products`, run still succeeds, watermark advances | One malformed variant must not stall the other 2000 |
| Row locked / request limit (`UNABLE_TO_LOCK_ROW`, 429) | Retried, then the single record is retried on its own | Transient; the batch should not be lost |
| Run hits the time budget | Exits **succeeded** with the page cursor saved | Better than being killed by the timeout and marked `timed_out` |
| Process killed / daemon restarted mid-window | Next run resumes from the saved page cursor | The watermark only moves when a window is fully drained |
| A page cursor does not advance | The run aborts with a clear error rather than looping | A repeated cursor would spin until the timeout |
| Missing secret in the daemon environment | Run fails before Python starts, and is **not** retried | Caught by Otter, not by this code |
| Daemon started before `otter.env` was written | Every run fails instantly with `requires secrets that are not available` | The daemon's environment is read once, at startup |
| No active release, or an edited file not re-released | `otter run` refuses with `has no active release` (409); an activated release keeps running the old code | Runs execute an immutable release, not the working tree |
| `python.path` missing from the release snapshot | The release is marked invalid and runs fail before Python starts | Shared code is captured at release time; re-run `otter release` |

---

## Gotchas

**One Product2 per variant, not per product.** A three-variant product becomes
three rows, named `"Tee - Small"`, `"Tee - Medium"` and `"Tee - Large"`. If your
Salesforce org expects one Product2 per Shopify product, this integration is the
wrong shape — and its `Name` rule is designed around that choice, not around
product-level rows.

**The watermark follows `Product.updatedAt`, not a variant's.** That is the
whole reason the root is `products`: a product rename does not bump its
variants' `updatedAt`, so a variant-level watermark would miss the rename
forever. The opposite direction is the one to check before extending the
mapping — if a variant-only edit (a SKU or price change) does not bump its
parent product's `updatedAt`, then mapping `sku` or `price` will not keep them
current. Today the mapping reads only product-level fields plus ids, so this is
invisible.

**A deleted product or variant is not deleted in Salesforce.** This integration
only upserts. A product removed in Shopify leaves its Product2 rows behind
forever. If that matters, map `IsActive` and set it to false from a delete
sweep — a separate integration.

**Protected customer data does not apply here.** Products and variants are not
protected customer data, so there is no approval step like the customer sync's.
An empty *customer* sync and an empty *product* sync have different causes.

**`Shopify_Variant_Id__c` must be External ID + Unique.** If it is only a text
field, every write fails with `INVALID_FIELD`. The field is also what makes
re-runs updates rather than duplicates, so it must be unique.

**Duplicates.** The upsert matches on `Shopify_Variant_Id__c` only. Product2
rows created by hand or by another tool, even with the same `ProductCode`, will
not be matched and you will get a second row. Pick a matching strategy before the
first run.

**The schema snapshot is not your org.** `mapping.py` validates against the
committed `schema/salesforce/product2.py`, not against your Salesforce. A field
that exists here but not there imports fine and then fails every write with
`INVALID_FIELD`. Create the fields or re-pull the schema.

**API limits.** A 5-minute incremental sync is small, but a first backfill is
not: Salesforce enforces a daily API request limit and Shopify enforces a
GraphQL cost budget, and one product page can expand into hundreds of variant
rows. Keep `MAX_PAGES_PER_RUN` modest and let the backfill take a few hours
rather than hammering both APIs.

**Repeated retries and the cron.** `retry.attempts` is 3 with a 30s initial
delay, chosen so a retry lands inside the next 5-minute tick. If the sync is
broken for a long time, failed runs and cron ticks can queue up; because every
run is watermark-based and idempotent, the redundant ones are fast no-ops and the
backlog drains, but `otter status` will show queue depth while it does.

**`CERTIFICATE_VERIFY_FAILED` on a developer Mac.** The python.org macOS
installers ship no CA store, so *every* HTTPS request from that interpreter fails
— Shopify and Salesforce alike. `otter_connectors/http.py` detects this and falls
back to `certifi`'s bundle, so no configuration is needed.

**`SHOPIFY_API_VERSION` matters.** Shopify releases quarterly and retires
versions; the manifest pins one deliberately. Bump it on your own schedule and
watch the first run after the change.

---

## Later improvements

- **Map more variant fields.** `sku` and `price` are already selected by both
  queries and unused, so `ProductCode` and `StockKeepingUnit` are one mapping
  line each. `Description` is not selected yet, so it needs a line in each query
  too. This is the change the integration is shaped for.
- **A variant-level watermark**, if the mapped fields become variant-specific,
  so a variant-only edit is not missed. It would need both watermarks, because
  the product-level one is what catches renames.
- **A delete sweep.** Diff the Product2 rows carrying `Shopify_Product_Id__c`
  against Shopify and clear `IsActive` for the ones that are gone.
- **Shopify webhooks.** Register `products/create` and `products/update`
  webhooks pointing at Otter's `POST /v1/hooks/<integration>` for near-real-time
  sync. You still want this polling integration as the reconciliation safety net,
  since webhooks can be dropped.
- **Bulk API for the backfill**, as with the customer sync.
