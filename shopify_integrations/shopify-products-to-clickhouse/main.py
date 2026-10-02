"""Pull Shopify products and their variants into ClickHouse.

A table of contents rather than a program. ``source.py`` says what to *read*,
``mapping.py`` where it *lands*, ``settings.py`` what it is configured to do,
``clickhouse_sync.py`` holds the page loop and its sink, ``sync_window.py`` the
resumable window, and ``run_state.py`` what each run records about itself. This
file reads no environment variables and keeps no durable state of its own.

The reusable pieces -- the Shopify and ClickHouse clients, the resumable
watermark -- live in ``otter_connectors``. The runtime primitives (scheduling,
retries, timeouts, durable state, logs, run history) are Otter's and are not
reimplemented here.
"""

import time

from otter import run
from otter_connectors.clients import clickhouse_client, shopify_client
from otter_connectors.timeutil import utcnow

from clickhouse_sync import ClickHouseSink, DryRunSink, drain
from run_state import report_run, report_start
from settings import Settings
from sync_window import begin_window, finalize_window


@run
def main(ctx):
    cfg = Settings.load()

    # The store is an explicit argument rather than something the factory reads:
    # a Shopify client is bound to one store, so naming it here is what would
    # make a future multi-store run a loop instead of a rewrite. Credentials are
    # per-app, so the factory reads those. The ClickHouse endpoint is named for
    # the same reason -- a client talks to exactly one database.
    shopify = shopify_client(cfg.shopify_store)
    client = clickhouse_client(url=cfg.clickhouse_url)

    # A dry run gets a sink with the same interface, so the page loop has no
    # branch and there is no None to guard.
    sink = (
        DryRunSink(ctx.log)
        if cfg.dry_run
        else ClickHouseSink(client, cfg.products_table, cfg.variants_table, ctx.log)
    )

    started = utcnow()
    deadline = time.monotonic() + cfg.budget_seconds

    window = begin_window(ctx.state, cfg, started)
    report_start(
        ctx.log,
        cfg,
        window.start,
        resumed=window.resumed,
        had_watermark=window.had_watermark,
    )

    result = drain(ctx.log, cfg, shopify, sink, window, deadline=deadline)

    # Close the window before reporting it, so the summary names what was
    # actually persisted rather than what the run intended to persist.
    finalize_window(ctx.log, window, result, started)
    report_run(ctx, result, started, window.start, dry_run=cfg.dry_run)
