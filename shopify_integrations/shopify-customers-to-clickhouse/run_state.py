"""What a run records about itself: durable state, and the logs describing it.

"State" is in the name because this module *persists* as well as logs.
``report_run`` writes ``last_run``, which is read back by ``otter state get`` and
by anything watching this job, so it is part of the integration's contract rather
than just output.
"""

from otter_connectors.timeutil import to_iso, utcnow


def report_start(log, cfg, window_start, *, resumed, had_watermark):
    """Log the window's starting position and sync configuration."""
    if resumed:
        log.info("resuming interrupted window", window_start=to_iso(window_start))
    elif not had_watermark:
        log.info("first run; backfilling", window_start=to_iso(window_start))

    log.info(
        "sync starting",
        store=cfg.shopify_store,
        customers_table=cfg.customers_table,
        window_start=to_iso(window_start),
        dry_run=cfg.dry_run,
        page_size=cfg.page_size,
    )


def report_window_progress(log, window_start, result, *, dry_run):
    """Report where the window ended up, and whether anything was persisted."""
    if dry_run:
        log.info("dry run: watermark not advanced", window_start=to_iso(window_start))
        if not result.complete:
            # Otherwise a rehearsal that stopped early reads like a complete one.
            log.info(
                "dry run: window not fully drained; nothing persists",
                cursor=result.cursor,
                pages=result.pages,
            )
        return

    if not result.complete:
        log.info(
            "window partially drained; next run continues",
            cursor=result.cursor,
            pages=result.pages,
        )


def report_run(ctx, result, started, window_start, *, dry_run):
    """Persist run metadata and log the same totals."""
    totals = {
        "pages": result.pages,
        "customers_fetched": result.customers_fetched,
        "customers_written": result.customers_written,
        "complete": result.complete,
    }
    ctx.state.set(
        "last_run",
        {
            **totals,
            "started_at": to_iso(started),
            "finished_at": to_iso(utcnow()),
            "window_start": to_iso(window_start),
            "dry_run": dry_run,
        },
    )
    ctx.log.info("sync finished", **totals)
