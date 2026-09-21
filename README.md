# otter_examples

Example [Otter](https://github.com/tkoizumi/otter) integrations: small, runnable,
and commented more heavily than production code would be, because the point is
to be read as much as run.

Today there are two. They share the same Shopify and Salesforce clients, the same
`otter.env`, and the same daemon, and neither knows the other exists.

| Integration | What it does |
| --- | --- |
| [`shopify_customer_to_salesforce_contact`](shopify_integrations/shopify_customer_to_salesforce_contact/README.md) | Upserts Shopify customers into Salesforce Contacts every five minutes, incrementally and idempotently. |
| [`shopify-product-to-salesforce-product`](shopify_integrations/shopify-product-to-salesforce-product/README.md) | Upserts Shopify product variants into Salesforce Product2 every five minutes, incrementally and idempotently. |

## Layout

```text
otter_examples/                                 this git repository, and the Otter project root
├── otter.env                                   secrets; 0600, gitignored, loaded by `otter start`
├── otter.env.example                           committed template — copy it, never fill it in
├── .gitignore                                  keeps every env file out of git
├── .otter/                                     machine-local state: SQLite, run history, staged
│                                               releases, the prepared interpreter; created on the
│                                               first `otter start`, gitignored
└── shopify_integrations/                       a grouping directory, not a project boundary
    ├── lib/python/                             shared code, snapshotted into each release
    │   ├── otter_connectors/                   Shopify + Salesforce clients, watermark, config
    │   ├── otter_schema/                       schema references, and the puller that writes them
    │   └── tests/
    ├── shopify_customer_to_salesforce_contact/
    │   ├── otter.yaml                          when and how it runs
    │   ├── main.py  source.py  mapping.py      orchestration / read / write
    │   ├── tests/test_logic.py
    │   └── README.md                           the full setup and operations guide
    └── shopify-product-to-salesforce-product/
        ├── otter.yaml                          when and how it runs
        ├── main.py  source.py  mapping.py      orchestration / read / write
        ├── settings.py  product_sync.py        every knob / the page loop
        ├── sync_window.py  run_state.py        the resumable window / run bookkeeping
        ├── queries/                            products.graphql, product-variants.graphql
        ├── schema/                             pulled Shopify + Salesforce schema references
        ├── tests/                              test_main.py, test_settings.py, test_source.py
        └── README.md                           the full setup and operations guide
```

The two integrations are structurally similar but not identical: the product
sync splits the page loop, the window and the run bookkeeping into their own
modules, and keeps its GraphQL documents in `queries/` next to a pulled `schema/`
that the mapping names fields through. Both are self-contained directories, and
either can be read on its own.

Two things about this layout are worth knowing up front.

**The project root is the repository root.** Otter identifies a project by the
nearest directory holding `.otter/`, `.git` or `go.mod`, then discovers every
`otter.yaml` beneath it, however deeply nested. `shopify_integrations/` groups
related integrations; it is not a separate project and holds no `.otter/` of its
own. Run `otter` commands from the repository root — they also work from
anywhere beneath it, because the search walks upward.

**An integration is addressed by its manifest `name:`.** For the customer sync
that is `shopify_customer_to_salesforce_contact`, and for the product sync it is
`shopify-product-to-salesforce-product`. Each matches the directory it lives in —
one spelling with underscores, one with dashes — and each is the string every CLI
command takes: `otter validate shopify_customer_to_salesforce_contact`,
`otter release shopify-product-to-salesforce-product`. A path works too —
`otter validate shopify_integrations/shopify_customer_to_salesforce_contact` —
and prints the name it resolved.

## Prerequisites

| | |
| --- | --- |
| `otter` | 0.1.9 or newer — `otter --version` |
| Python | none required on the host. Both manifests use `python.mode: managed`, so Otter prepares the pinned interpreter from `.python-version` and the locked `uv.lock` into `.otter/data/` at release time. |
| Shopify | a Dev Dashboard app installed on a store in the same organization. The customer sync needs `read_customers` **and** protected customer data access approved; the product sync needs `read_products`, which is not protected data. |
| Salesforce | a connected app with the client credentials flow and a Run As user, plus the external ID fields the mappings upsert on: `Shopify_Customer_Id__c` on Contact, and `Shopify_Variant_Id__c` (plus `Shopify_Product_Id__c`) on Product2. |

Setting those up is the bulk of the work, and each integration's README walks
through its own half. The two integrations are independent: you can run either
one alone, and neither needs the other's Salesforce fields, scopes or settings.

## Quick start

```bash
git clone https://github.com/tkoizumi/otter_examples.git
cd otter_examples

# 1. Secrets. The template is committed; the file you fill in is not.
cp otter.env.example otter.env
chmod 600 otter.env
$EDITOR otter.env        # SHOPIFY_CLIENT_ID/_SECRET, SALESFORCE_CLIENT_ID/_SECRET

# 2. Point each manifest at your own stores (otter.yaml, env: block):
#    SHOPIFY_STORE, SALESFORCE_INSTANCE_URL, BACKFILL_FROM

# 3. Validate, release, run. Commands take the manifest name, or a path.
otter validate shopify_customer_to_salesforce_contact
otter validate shopify-product-to-salesforce-product
otter release --all
otter start --detach
otter run shopify_customer_to_salesforce_contact
otter run shopify-product-to-salesforce-product
```

`otter release --all` snapshots and activates every integration in the project;
naming one is equally fine if you only want one of them running. The same is
true of the cron triggers — a daemon running with both manifests fires both
schedules.

There is no `otter init` step: the manifests and the template are already
committed, and `.otter/` is machine-local state that `otter start` creates. A
fresh clone has no `.otter/` — that is expected, and the `.git` at the root is
what identifies the project until the first start creates one. The first
`otter start` prints the `project`, `integrations` and `data` paths it resolved;
they should all point at the checkout.

Runs execute the **active release**, an immutable snapshot of the integration
plus its shared `lib/python`. Editing a file changes nothing until you
`otter release` again, and `otter run` refuses outright (`409 has no active
release`) if an integration was never released.

## How configuration and secrets reach an integration

There are three places a setting can come from, and they are not
interchangeable:

| Where | Holds | Visible in `otter inspect`? | Read when? |
| --- | --- | --- | --- |
| `otter.yaml` → `env:` | non-secret, per-integration settings | **yes, printed in full** | at release |
| `otter.yaml` → `secrets:` | a list of **names only**, no values | names only | — |
| `otter.env` (repository root) | the values, and any operational knob | no | once, when the daemon starts |

The daemon's whole environment is passed to the child process, so an operational
knob such as `DRY_RUN` or `PAGE_SIZE` works from `otter.env` without touching
the manifest. Where the manifest also defines a key, **the manifest wins**; a
manifest value can pull from the daemon environment with `${VAR}`:

```yaml
env:
  SHOPIFY_STORE: ${PROD_SHOPIFY_STORE}
```

The one consequence worth memorising:

> **Secrets and knobs are read when the daemon starts, not when a run starts.**
> After editing `otter.env`, restart: `otter stop && otter start --detach`.

`otter start` loads `otter.env` and `otter.daemon.env` from the project root; a
variable already exported in your shell wins over the file. `otter serve` and
bare `otterd` do **not** load either file.

## Keeping secrets out of git

`.gitignore` at the repository root ignores every env file — `.env`, `.env.*`,
`*.env`, `*.env.*` — templates included. A `.env.example` that someone pastes
real values into is a secret like any other, so there is deliberately no
`!*.env.example` re-include. `otter.env.example` stays committed because it was
added before those rules existed; a new template has to be added with
`git add -f`, which is the point.

Two things that follow:

- **`.gitignore` only filters untracked files.** It does nothing for a path
  already in the index; use `git rm --cached <path>` (which keeps the
  working-tree copy) and commit.
- **It is a path filter, not a secret scanner.** A credential pasted into
  `main.py`, a fixture or a log is still committable. For a repo that gets
  pushed, add [gitleaks](https://github.com/gitleaks/gitleaks) or
  [trufflehog](https://github.com/trufflesecurity/trufflehog) as a content-level
  backstop.

`otter init` scaffolds a `.gitignore` for a *new* workspace, and on 0.1.10 that
scaffold already matches these patterns — including the absence of a template
re-include. The only difference here is that this repository committed its
template before the rules existed.

If a real credential is ever committed, **rotate it**. Rewriting the commit
stops publication; only rotation makes the leaked value worthless. GitHub's push
protection will refuse such a push, which is the system working — the
`unblock-secret` link in that message asserts a false positive and should not be
used for a live credential.

## Troubleshooting

Errors this workspace has actually produced, and what they mean:

| Message | Cause | Fix |
| --- | --- | --- |
| `requires secrets that are not available: …` | The daemon's environment lacks the key. Almost always the daemon was started before `otter.env` was written. | `otter stop && otter start --detach`, then confirm uptime in `otter status` is newer than the file. |
| `has no active release` (HTTP 409) | The integration was never released, or `.otter` was cleared. | `otter release <integration>` |
| `python.path[0] "…" does not exist` | The active release predates a change to the shared library layout, or `otter.yaml` points somewhere wrong. | `otter validate <dir>`, then `otter release <integration>` again. |
| `no project found (no .otter, .git or go.mod in this directory or above)` | You are outside the project, or working from a source tarball with no `.git` and no `.otter/` yet. | Run from the repository root, or pass `--data` and `--integrations`. |
| Push rejected: `GH013 … Push cannot contain secrets` | A credential is in a commit's tree. `.gitignore` cannot retract it. | `git rm --cached <path>` + `git commit --amend`, then **rotate the credential**. |

`otter logs <run-id>` and `otter state get <integration> last_run` usually
identify a problem faster than the daemon log; the daemon log is at
`.otter/serve/serve.log`.

## Adding another integration

An integration is any directory containing an `otter.yaml`, anywhere under the
project root. To add one that reuses the vendor clients:

1. Create `shopify_integrations/<name>/` with `otter.yaml`, `main.py`, and a
   `python:` block pointing at the shared library:

   ```yaml
   python:
     mode: managed
     path:
       - ../lib/python
   ```

   `.python-version`, `pyproject.toml` and `uv.lock` sit beside it for managed
   mode; copy them from either existing integration. The `../lib/python` path is
   relative to the integration directory, which is why the shared library stays
   a sibling of the integrations and does not move with the project root.
2. Import from `otter_connectors` rather than copying client code — see
   [`lib/python/README.md`](shopify_integrations/lib/python/README.md) for what
   is in there and why it is not part of the Otter SDK. If you want the mapping
   to name fields through pulled schema references instead of bare strings, the
   product sync is the example to copy from, and
   [`otter_schema/README.md`](shopify_integrations/lib/python/otter_schema/README.md)
   documents the puller.
3. Add only the **names** of any new credentials to `secrets:`, and their values
   to `otter.env` at the repository root. One file serves every integration in
   the project.
4. `otter validate shopify_integrations/<name>`, `otter release --all`,
   `otter run <manifest-name>`.

## Where to read more

- [shopify_customer_to_salesforce_contact README](shopify_integrations/shopify_customer_to_salesforce_contact/README.md) —
  Shopify and Salesforce setup, the full configuration reference, field mapping,
  failure behaviour and gotchas for the customer sync.
- [shopify-product-to-salesforce-product README](shopify_integrations/shopify-product-to-salesforce-product/README.md) —
  the same for the product sync, plus why its root is `products` rather than
  `productVariants`, and how its GraphQL documents and pulled schema fit together.
- [`lib/python/README.md`](shopify_integrations/lib/python/README.md) — the shared
  Shopify and Salesforce clients, and how to reuse them.
- [`otter_schema/README.md`](shopify_integrations/lib/python/otter_schema/README.md) —
  schema references for mappings, and the puller that writes them.
- [`.gitignore`](.gitignore) — the secret-exclusion rules, with the reasoning
  inline.
- `otter --help`, and `otter <command> --help` for the exact flags of any
  command used above.
