"""Read a page of Shopify orders, map them, and insert them into ClickHouse.

The loop is the same shape as the sibling integration's, for the same reason: a
run drains whole pages, checkpoints the cursor after each one, and stops early
when its budget is gone rather than being killed mid-page. What differs is the
sink: orders has one destination table and no nested connection, so there is one
``upsert`` instead of two. ``ReplacingMergeTree`` keyed by Shopify's own id makes
a replayed page converge instead of duplicating, so a crash after the insert and
before the checkpoint costs a re-read rather than a duplicate.
"""

import time
from dataclasses import dataclass

from otter_connectors.shopify import ShopifyError
from otter_connectors.timeutil import utcnow

from mapping import Tenancy, order_row
from source import fetch_page


class ClickHouseSink:
    """Writes mapped rows through the ClickHouse client.

    It carries the destination table so the page loop names a *thing to sync*
    rather than an INSERT target, which is what lets ``DryRunSink`` stand in with
    the same interface.
    """

    def __init__(self, client, table, log):
        self.client = client
        self.table = table
        self.log = log

    def upsert(self, rows):
        """Insert one page's rows and return how many were sent.

        An insert is all-or-nothing, and a failure raises rather than being
        collected per record: ClickHouse's message names the column and the value
        that broke it, so the useful response is to stop and read it, not to
        carry on with the next page. The cursor has not advanced, so the run
        retries the same page.
        """
        written = self.client.insert(self.table, rows)
        self.log.info("clickhouse insert", table=self.table, rows=written)
        return written


class DryRunSink:
    """Stands in for ``ClickHouseSink``: reports what would be written.

    Taking the same ``upsert(rows)`` signature is what lets a dry run be a sink
    rather than a flag threaded through the loop. There is no ``None`` for a
    later edit to trip over, and no branch in the page loop.

    It carries the table for the same reason the real sink does: "would insert
    17 rows" is only half the answer, and the half it omits is the one that says
    whether the job is pointed at the table you meant.
    """

    def __init__(self, table, log):
        self.table = table
        self.log = log

    def upsert(self, rows):
        self.log.info(
            "dry run: would insert",
            table=self.table,
            rows=len(rows),
            first=rows[0] if rows else None,
        )
        return len(rows)


@dataclass
class DrainResult:
    """What one run did, for the summary it logs and the state it persists."""

    pages: int
    orders_fetched: int
    orders_written: int
    cursor: str | None
    complete: bool


def drain(log, cfg, shopify, sink, window, *, deadline, now=time.monotonic):
    """Process pages and checkpoint progress until drained or out of budget.

    ``now`` is injectable so the budget path is testable without sleeping.
    """
    window_start = window.start
    cursor = window.cursor
    # The destination's tenant ids are the same for every row this run writes, so
    # they are resolved once here rather than read per record.
    tenancy = Tenancy(cfg.workspace_id, cfg.connection_id, cfg.dataset_id)
    orders_fetched = orders_written = 0
    pages = 0
    complete = False

    while pages < cfg.max_pages:
        if now() > deadline:
            log.warning(
                "run budget reached; will continue on the next tick",
                pages=pages,
                orders=orders_fetched,
                budget_seconds=cfg.budget_seconds,
            )
            break

        orders, page_info = fetch_page(
            shopify,
            window_start=window_start,
            page_size=cfg.page_size,
            cursor=cursor,
        )
        pages += 1

        if not orders:
            complete = True
            break

        synced_at = utcnow()
        rows = [order_row(order, synced_at, tenancy) for order in orders]

        # The orders themselves are deliberately not logged: one page is a
        # hundred nodes, each carrying two addresses and six money sets. The
        # counts and the first order name are what you actually read.
        log.info(
            "shopify page",
            page=pages,
            orders=len(orders),
            first=orders[0].get("name"),
        )

        orders_written += sink.upsert(rows)
        orders_fetched += len(rows)

        next_cursor = page_info.get("endCursor")
        if not page_info.get("hasNextPage"):
            complete = True
            break
        if not next_cursor or next_cursor == cursor:
            raise ShopifyError(
                "Shopify returned a non-advancing page cursor; aborting to avoid a loop"
            )

        cursor = next_cursor
        # Checkpoint after writing: a crash past this point resumes at the next
        # page. Whether that is durable is the window's decision, since a dry run
        # persists nothing.
        window.checkpoint(cursor)
        log.info("page synced", page=pages, orders=len(rows))

    return DrainResult(
        pages=pages,
        orders_fetched=orders_fetched,
        orders_written=orders_written,
        cursor=cursor,
        complete=complete,
    )
