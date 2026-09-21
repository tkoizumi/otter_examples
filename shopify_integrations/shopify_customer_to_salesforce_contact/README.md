# shopify_customer_to_salesforce_contact

Upserts Shopify customers into Salesforce every five minutes, incrementally and
idempotently.

```text
Shopify Admin API (GraphQL)          Otter                      Salesforce REST API
  customers(updated_at:>watermark)  ──►  child Python process  ──►  composite/sobjects upsert
  cursor pagination                     state: watermark,           by Shopify_Customer_Id__c
                                        page cursor, dead letters
```

Runtime primitives (scheduling, the watermark, retries, timeouts, logs, run
history) are all Otter's. The vendor clients are not — they live in a shared
library, so this file holds only what is specific to this sync.

> **The integration is addressed by the manifest's `name:`.** It is
> `shopify_customer_to_salesforce_contact`, which matches this directory, and
> that is the string every CLI command takes. From inside this directory,
> `otter validate .` does the same without naming it.

## Where this sits in the workspace

One integration directory inside the `shopify_integrations` grouping directory.
The Otter project root is the repository root, which is where `otter.env` and
`.otter/` live. Paths below are relative to this directory unless stated
otherwise.

```text
otter_examples/                                the Otter project root
├── otter.env                                  secrets; 0600, gitignored, loaded by `otter start`
├── otter.env.example                          the committed template to copy
├── .gitignore                                 keeps every env file out of git
└── shopify_integrations/                      a grouping directory, not a project boundary
    ├── lib/python/                            shared code, snapshotted into each release
    │   ├── otter_connectors/
    │   │   ├── shopify.py        Shopify GraphQL client: client credentials grant,
    │   │   │                     throttle backoff, Relay cursor paging
    │   │   ├── salesforce.py     Salesforce REST client: OAuth, batched upsert by
    │   │   │                     External ID, picklist-aware values, address fallback
    │   │   ├── records.py        record building: dotted paths, join, drop empties,
    │   │   │                     truncate, mapping validation
    │   │   ├── checkpoint.py     Watermark: resumable "what have I processed?" state
    │   │   ├── config.py         env / env_int / env_bool / require_env
    │   │   ├── http.py           the CA-aware opener both clients share
    │   │   ├── timeutil.py       utcnow / to_iso / parse_iso
    │   │   └── errors.py         ConnectorError, ConfigError
    │   └── otter_schema/         field references used by mappings
    └── shopify_customer_to_salesforce_contact/    <- you are here
        ├── otter.yaml        when and how it runs
        ├── source.py         what we read from Shopify: the query and its paging
        ├── mapping.py        where it lands: the Contact mapping and field lengths
        ├── main.py           orchestration: clients, watermark, page loop, dead letters
        └── tests/test_logic.py
```

**Editing the integration usually means editing `source.py` and `mapping.py`
only.** Adding a field is a line in each (the query, and the mapping) plus a
length in `MAX_FIELD_LENGTH`; `main.py` should not need touching, and
`tests/test_logic.py` pins the mapping down.

`mapping.py` is deliberately pure — no environment reads, no I/O, nothing
imported from `main` or `source` — so it is testable on a plain dict and could
be lifted into a shared package unchanged.

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
- [Keeping secrets out of git](#keeping-secrets-out-of-git)
- [How it behaves when things go wrong](#how-it-behaves-when-things-go-wrong)
- [Field mapping](#field-mapping)
- [Gotchas](#gotchas)
- [Later improvements](#later-improvements)

---

## Before you start

Three things must exist before the first run. None of them are things Otter can
do for you.

### 1. A Shopify app in the Dev Dashboard

Two things have changed in Shopify's auth story, and both matter here:

- **The Admin REST API is a legacy API** ([shopify.dev](https://shopify.dev/docs/api/admin-rest/2025-10/resources/customer)),
  so this integration uses the GraphQL Admin API.
- **Admin-created custom apps can no longer be created.** There is no longer a
  token to copy out of the Shopify admin. A server-side integration acting on
  your own stores uses the
  [client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant):
  it exchanges its own client ID and secret for an access token that is valid
  for **24 hours**, and renews it by repeating the same request. This
  integration does that automatically at the start of every run, so there is no
  long-lived Shopify token anywhere.

Set the app up:

1. Create an app in the **[Dev Dashboard](https://dev.shopify.com/dashboard/)**
   (not the Shopify admin's *Develop apps* page).
2. On the app's **version**, select the `read_customers` scope and release it.
   Scopes live on the version, so adding one later means releasing a new version
   and approving the change on the store.
3. **Install the app on your store.**
4. Copy the **Client ID** and **Client secret** from the app's **Settings**.
   These are what go in `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET`.

**The catch that stops most people:** the client credentials grant only works
when the app and the store belong to the **same Shopify organization**. Having
the app installed is not enough — the store must appear under **Dev stores** in
that organization in the Dev Dashboard. A store belonging to a client, or a dev
store created from the Shopify admin rather than the Dev Dashboard, is not in
your organization, and every token request fails with:

```text
Oauth error shop_not_permitted: Client credentials cannot be performed on this shop.
```

If your store is not in your organization, client credentials cannot reach it.
Distribute the app with **custom distribution** so a merchant installs it, and
switch to the authorization code grant (which needs a redirect flow and a stored
refresh token — a different design from this one).

5. **Request protected customer data access.** Name, email, phone and address
   are protected customer data. Without approval for your app, Shopify returns
   those fields as `null` and the sync silently creates Contacts with only a
   last name. This is the single most common cause of an "empty" sync that
   reports success.

Then confirm the grant and the query work before wiring anything up:

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
echo "$TOKEN"   # 32 hex chars, plus scope and expires_in=86399 in the response

# 2. Use it, and confirm protected customer data is approved
curl -sS -X POST "https://$SHOPIFY_STORE/admin/api/2026-07/graphql.json" \
  -H "X-Shopify-Access-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  --data @- <<'JSON'
{"query":"query { customers(first: 2, sortKey: UPDATED_AT) { nodes { id email firstName lastName phone updatedAt } } }"}
JSON
```

You should see real names and emails. If they are `null`, fix the protected-data
approval first. If GraphQL rejects `sortKey` alongside `query` on your API
version, set `SHOPIFY_SORT_KEY` (see [Configuration](#configuration)).

### 2. A Salesforce External ID field

The upsert keys on a custom field that must be marked **External ID** and
**Unique**:

1. Setup → Object Manager → **Contact** → Fields & Relationships → **New**
2. Type **Text**, length **255**
3. Field Label `Shopify Customer Id`, Field Name `Shopify_Customer_Id__c`
4. Tick **External ID** and **Unique**, then Save

If the field is not marked External ID, every write fails with
`INVALID_FIELD`. Rename it and update `SALESFORCE_EXTERNAL_ID_FIELD` if you
prefer a different name.

### 3. Salesforce OAuth credentials

This integration defaults to the **client credentials** flow, which is the
right choice for an unattended server: no password, no security token, nothing
to rotate monthly.

1. Setup → App Manager → **New Connected App**
2. Enable OAuth Settings. Any callback URL will do
   (`https://login.salesforce.com/services/oauth2/success`).
3. OAuth scope: **Manage user data via APIs (`api`)**
4. Tick **Enable Client Credentials Flow**, then set the **Run As** user to a
   dedicated integration user that can read/write Contacts.
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
not started: integration shopify_customer_to_salesforce_contact requires
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
  environment. `otter.daemon.env` is the separate, daemon-wide file
  (`--daemon-env`) for the same mechanism.

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
| `SALESFORCE_OBJECT` | `Contact` | `Account` works but needs a different mapping. |
| `SALESFORCE_EXTERNAL_ID_FIELD` | `Shopify_Customer_Id__c` | Must be External ID + Unique. |
| `BACKFILL_FROM` | `2026-01-01T00:00:00Z` | Where the *first ever* run starts. |

### In the daemon's environment (operational knobs, set per deployment)

These are read with sensible defaults and are deliberately **not** in the
manifest, so exporting them for the daemon (or putting them in `otter.env`)
overrides them without editing a committed file. The manifest wins for any key
it also defines.

| Key | Default | Purpose |
| --- | --- | --- |
| `PAGE_SIZE` | `100` | Customers per Shopify page. |
| `MAX_PAGES_PER_RUN` | `20` | Upper bound on work per run (20 × 100 = 2000 customers). |
| `RUN_BUDGET_SECONDS` | `240` | Wall-clock budget, kept under the 300s timeout. |
| `OVERLAP_SECONDS` | `600` | How far back each window re-scans. |
| `SALESFORCE_BATCH_SIZE` | `200` | Records per composite call; `1` forces per-record writes. |
| `SYNC_ADDRESS` | `1` | Set `0` to skip mailing address fields. |
| `DRY_RUN` | `0` | Set `1` to read Shopify and log writes without touching Salesforce. |
| `SHOPIFY_SORT_KEY` | `UPDATED_AT` | Change if your API version rejects it. |
| `SHOPIFY_API_BASE` | derived from store + version | Override to point at a mock. |
| `SHOPIFY_TOKEN_URL` | `https://<store>/admin/oauth/access_token` | Override to point at a mock. |
| `SALESFORCE_USERNAME` / `SALESFORCE_PASSWORD` | unset | Password flow only. |

Because these arrive through the daemon's environment, changing one means a
restart, exactly like a secret.

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
anyway. The integration therefore simply requests a token at the start of every
run: about 288 extra requests a day on a 5-minute schedule, nothing persisted,
and no stale-token failure mode. It refreshes once mid-run if Shopify answers
`401`/`403`, which covers a secret rotation or a token lapsing between pages.

If you would rather not make that call every 5 minutes, cache
`access_token` + `expires_in` in Otter state and reuse it until it is close to
expiry. It is a small change to `ShopifyClient.access_token()` — but you then
own the staleness problem, which is why it is not the default.

---

## Running it

All commands run from the project root (the repository root) unless noted. The
integration is addressed by its manifest name.

```bash
# 1. Check the manifest. Runs locally, needs no daemon, and catches a bad
#    python.path or a malformed env block before anything else. The argument is
#    the manifest name; a path (shopify_integrations/<dir>) works too.
otter validate shopify_customer_to_salesforce_contact

# 2. Snapshot the integration and its shared code into an immutable release and
#    activate it. Runs execute the ACTIVE RELEASE, so an edit to main.py,
#    source.py or mapping.py is not live until this runs again.
otter release shopify_customer_to_salesforce_contact

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
otter run shopify_customer_to_salesforce_contact
otter logs <run-id> | head -40
```

Look for `dry run: would upsert` lines containing real names and emails, then
confirm nothing was recorded:

```bash
otter state get shopify_customer_to_salesforce_contact sync_cursor
# otter: shopify_customer_to_salesforce_contact/sync_cursor is not set
```

**Then for real.** Restart without `DRY_RUN` and run again. The first run
backfills from `BACKFILL_FROM`, `MAX_PAGES_PER_RUN` pages at a time. If you have
more customers than fit in one run it exits **succeeded** with
`complete: false`, and the next run continues from the saved page cursor — no
data is lost and nothing is written twice. Watch progress:

```bash
otter state get shopify_customer_to_salesforce_contact last_run
otter state get shopify_customer_to_salesforce_contact in_progress_cursor
```

Once a window drains, the watermark advances and later runs only pick up
customers changed since. Confirm with a second run — it should fetch nothing:

```bash
otter logs "$(otter run shopify_customer_to_salesforce_contact)" | grep 'sync finished'
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
minutes. To work without the schedule first, comment the `trigger:` block out
and restart; manual runs work either way.

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
otter inspect shopify_customer_to_salesforce_contact               # config, cron, next fire time
otter runs --integration shopify_customer_to_salesforce_contact --limit 20
otter logs <run-id> --follow
otter state get shopify_customer_to_salesforce_contact last_run
otter state get shopify_customer_to_salesforce_contact failed_customers
otter state get shopify_customer_to_salesforce_contact failed_total
```

**Re-read the dead letters.** Records Salesforce permanently rejected are kept
in `failed_customers` (most recent 100) with the exact error; `failed_total`
counts all of them. Fix the cause — usually a State/Country picklist — then
re-sync those customers by rewinding the watermark:

```bash
otter state set shopify_customer_to_salesforce_contact sync_cursor '"2026-01-01T00:00:00Z"'
otter state delete shopify_customer_to_salesforce_contact in_progress_cursor
otter state delete shopify_customer_to_salesforce_contact in_progress_window_start
otter run shopify_customer_to_salesforce_contact
```

Rewinding is safe: every write is an upsert, so re-syncing updates the same
Contacts rather than duplicating them.

**Force a full re-sync.** Same as above with `BACKFILL_FROM`'s value.

**Pause the integration.** Comment out the `trigger:` block, `otter release` it
again, and restart the daemon; manual runs still work. Queued and running jobs
are unaffected.

**Ship a code change.** Edit `source.py` / `mapping.py`, run `otter validate`,
then `otter release` again. Until you do, runs keep executing the previous
release.

---

## Keeping secrets out of git

The rule that matters: **`.gitignore` only filters untracked files.** Adding a
pattern does nothing for a file that is already in the index — it has to be
removed from the index explicitly:

```bash
git rm --cached path/to/file        # keeps the working-tree copy
```

Two traps specific to this workspace:

- **A filled-in template is a secret.** `*.env.example` is ignored here, so a
  `.env.example` cannot be committed even if someone pastes real values into it.
  The committed template is `otter.env.example` at the repository root, and it
  must stay free of real values.
- **`.env` files are ignored, but nothing else is.** A credential pasted into
  `main.py`, a test fixture or a log is still committable. `.gitignore` is a path
  filter, not a secret scanner; add a content-level backstop
  ([gitleaks](https://github.com/gitleaks/gitleaks),
  [trufflehog](https://github.com/trufflesecurity/trufflehog)) if this repo is
  pushed anywhere.

GitHub's push protection blocks a push whose commits contain a live credential,
which is the desired outcome but is not a substitute for rotation: **if a real
secret is ever committed, rotate it.** Rewriting the commit stops publication;
only rotation makes the leaked value worthless.

Note that `otter init` on 0.1.10 scaffolds a `.gitignore` that already matches
the rules at the repository root, including the absence of a template
re-include. `otter.env.example` stays committed only because it predates those
rules; a new template has to be added with `git add -f`.

---

## How it behaves when things go wrong

| Situation | What happens | Why |
| --- | --- | --- |
| Shopify 5xx or unreachable | Retried in-process, then the run fails and Otter retries with backoff | Infrastructure blip; the run is safe to repeat |
| Shopify throttles (GraphQL `THROTTLED`) | Sleeps until the leaky bucket can afford the next query | Avoids burning the retry policy on rate limits |
| Access token lapses or is revoked mid-run | Refreshed once via the client credentials grant, then the page is retried | 24 hour tokens, and a secret rotation invalidates them early |
| Credentials wrong, or store not in the app's organization | Run fails immediately with `shop_not_permitted` or the raw Shopify error | Configuration error; retries cannot help |
| Missing `read_customers` scope or protected-data approval | Request succeeds but names/emails come back `null` | Shopify returns what the scopes allow, so this looks like success |
| One customer rejected by Salesforce (`INVALID_FIELD`, bad picklist) | Recorded in `failed_customers`, run still succeeds, watermark advances | One malformed customer must not stall the other 2000 |
| Row locked / request limit (`UNABLE_TO_LOCK_ROW`, 429) | Retried, then the single record is retried on its own | Transient; the batch should not be lost |
| Run hits the time budget | Exits **succeeded** with the page cursor saved | Better than being killed by the timeout and marked `timed_out` |
| Process killed / daemon restarted mid-window | Next run resumes from the saved page cursor | The watermark only moves when a window is fully drained |
| Missing secret in the daemon environment | Run fails before Python starts, and is **not** retried | Caught by Otter, not by this code |
| Daemon started before `otter.env` was written | Every run fails instantly with `requires secrets that are not available` | The daemon's environment is read once, at startup |
| No active release, or an edited file not re-released | `otter run` refuses with `has no active release` (409); an activated release keeps running the old code | Runs execute an immutable release, not the working tree |
| `python.path` missing from the release snapshot | The release is marked invalid and runs fail before Python starts | Shared code is captured at release time; re-run `otter release` |

---

## Field mapping

| Shopify | Salesforce Contact |
| --- | --- |
| `id` (numeric part) | `Shopify_Customer_Id__c` (the upsert key) |
| `lastName` | `LastName` — required; falls back to `firstName`, then `Shopify Customer <id>` |
| `firstName` | `FirstName` |
| `email` | `Email` |
| `phone` | `Phone` |
| `defaultAddress.address1` + `address2` | `MailingStreet` |
| `defaultAddress.city` | `MailingCity` |
| `defaultAddress.provinceCode` | `MailingState` |
| `defaultAddress.zip` | `MailingPostalCode` |
| `defaultAddress.countryCodeV2` | `MailingCountry` |

Empty values are **omitted**, not sent as `null`, so a sync never wipes a field
someone filled in by hand — the trade-off is that clearing a phone number in
Shopify will not clear it in Salesforce.

To add a field, add a line to `contact_mapping()` — a source path, a
`(path, transform)` pair, or a callable — plus a length in `MAX_FIELD_LENGTH`
if the target field has one. The mapping is data, so it reads as the mapping
rather than as the plumbing around it:

```python
mapping = {
    external_id_field: contact_external_id,                       # logic
    "LastName": contact_last_name,                                # logic
    "FirstName": contact_first_name,                              # logic
    "Email": "email",                                            # source path
    "MailingStreet": joined("defaultAddress.address1",
                            "defaultAddress.address2"),
}
```

Anything that does not fit is just a Python callable, so there is no mapping
language to learn or outgrow. `tests/test_logic.py` pins the mapping down — it
is the part that changes most often.

Anything beyond the standard fields above plus the external ID needs a matching
custom field in Salesforce first, or every write fails with `INVALID_FIELD`.

---

## Gotchas

**`CERTIFICATE_VERIFY_FAILED` on a developer Mac.** The python.org macOS
installers ship no CA store: `ssl.get_default_verify_paths().openssl_cafile`
points at a `cert.pem` that was never created, so *every* HTTPS request from
that interpreter fails — Shopify and Salesforce alike. `otter_connectors/http.py`
detects this and falls back to `certifi`'s bundle, so no configuration is needed.
The underlying interpreter can also be fixed properly with
`sudo "/Applications/Python <ver>/Install Certificates.command"` (worth doing if
you use that Python for anything else).

**State and Country picklists.** If your org has them enabled, `MailingState`
and `MailingCountry` are restricted picklists. `provinceCode`/`countryCodeV2`
(ISO codes) are normally what they expect, but a mismatched value yields
`INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST` and a dead letter. Either align the
picklist values or set `SYNC_ADDRESS=0`.

**Protected customer data.** Covered above, and worth repeating: without
approval, Shopify returns `null` for names, emails, phones and addresses and
the sync looks like it "works" while producing empty Contacts.

**Duplicates.** The upsert matches on `Shopify_Customer_Id__c` only. If the org
already has Contacts created by hand or by another tool, they will not be
matched and you will get a second Contact with the same email. Pick a matching
strategy before the first run — it is much easier than deduplicating later.

**API limits.** A 5-minute incremental sync is small, but a first backfill of
tens of thousands of customers is not: Salesforce enforces a daily API request
limit and Shopify enforces a GraphQL cost budget. Keep `MAX_PAGES_PER_RUN`
modest and let the backfill take a few hours rather than hammering both APIs.
For a one-off migration of a very large customer base, use Salesforce Bulk API
2.0 for the initial load and let this integration keep it current afterwards.

**Repeated retries and the cron.** `retry.attempts` is 3 with a 30s initial
delay, chosen so a retry lands inside the next 5-minute tick. If the sync is
broken for a long time, failed runs and cron ticks can queue up; because every
run is watermark-based and idempotent, the redundant ones are fast no-ops and
the backlog drains, but `otter status` will show queue depth while it does.

**`SHOPIFY_API_VERSION` matters.** Shopify releases quarterly and retires
versions; the manifest pins one deliberately. Bump it on your own schedule and
watch the first run after the change.

---

## Later improvements

- **Shopify webhooks.** Register `customers/create` and `customers/update`
  webhooks pointing at Otter's `POST /v1/hooks/<integration>` for
  near-real-time sync. You still want this polling integration as the
  reconciliation safety net, since webhooks can be dropped. It needs the daemon
  reachable from Shopify over TLS, with the generated webhook token.
- **Bulk API for the backfill**, as above.
- **Bidirectional sync.** Deliberately not attempted here. Two-way sync needs
  conflict resolution and a change-detection strategy; it is a project, not a
  config change.
- **A second destination.** If you later want the same customers in a
  warehouse too, that is a separate integration directory subscribing to the
  same Shopify data. Otter has no DAG to complicate it.
