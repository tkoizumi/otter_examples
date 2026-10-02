"""Read a page of Shopify products, map them, and insert them into ClickHouse.

The loop is the same shape as the sibling integration's, for the same reason: a
run drains whole pages, checkpoints the cursor after each one, and stops early
when its budget is gone rather than being killed mid-page. What differs is the
sink. ``ReplacingMergeTree`` keyed by Shopify's own id makes a replayed page
converge instead of duplicating, so a crash after the insert and before the
checkpoint costs a re-read rather than a duplicate.
"""

import time
from dataclasses import dataclass

from otter_connectors.shopify import ShopifyError
from otter_connectors.timeutil import utcnow

from mapping import Tenancy, product_row, variant_row
from source import fetch_page, variants_with_product


class ClickHouseSink:
    """Writes mapped rows through the ClickHouse client.

    It carries the table names so the page loop names a *thing to sync* rather
    than an INSERT target, which is what lets ``DryRunSink`` stand in with the
    same interface.
    """

    def __init__(self, client, products_table, variants_table, log):
        self.client = client
        self.products_table = products_table
        self.variants_table = variants_table
        self.log = log

    def upsert(self, table, rows):
        """Insert one page's rows and return how many were sent.

        An insert is all-or-nothing, and a failure raises rather than being
        collected per record: ClickHouse's message names the column and the value
        that broke it, so the useful response is to stop and read it, not to
        carry on with the next page. The cursor has not advanced, so the run
        retries the same page.
        """
        written = self.client.insert(table, rows)
        self.log.info("clickhouse insert", table=table, rows=written)
        return written


class DryRunSink:
    """Stands in for ``ClickHouseSink``: reports what would be written.

    Taking the same ``upsert`` signature is what lets a dry run be a client
    rather than a flag threaded through the loop. There is no ``None`` for a
    later edit to trip over, and no branch in the page loop.
    """

    def __init__(self, log):
        self.log = log

    def upsert(self, table, rows):
        self.log.info(
            "dry run: would insert",
            table=table,
            rows=len(rows),
            first=rows[0] if rows else None,
        )
        return len(rows)


@dataclass
class DrainResult:
    """What one run did, for the summary it logs and the state it persists."""

    pages: int
    products_fetched: int
    variants_fetched: int
    products_written: int
    variants_written: int
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
    products_fetched = variants_fetched = 0
    products_written = variants_written = 0
    pages = 0
    complete = False

    while pages < cfg.max_pages:
        if now() > deadline:
            log.warning(
                "run budget reached; will continue on the next tick",
                pages=pages,
                products=products_fetched,
                budget_seconds=cfg.budget_seconds,
            )
            break

        products, page_info = fetch_page(
            shopify,
            window_start=window_start,
            page_size=cfg.page_size,
            cursor=cursor,
        )
        pages += 1

        if not products:
            complete = True
            break

        synced_at = utcnow()
        product_rows, variant_rows = _build_rows(shopify, products, synced_at, tenancy)

        # The products themselves are deliberately not logged: each one carries
        # every variant beneath it, so one page would write a few hundred nodes
        # per line. The counts and the first title are what you actually read.
        log.info(
            "shopify page",
            page=pages,
            products=len(products),
            variants=len(variant_rows),
            first=products[0].get("title"),
        )

        products_written += sink.upsert(cfg.products_table, product_rows)
        variants_written += sink.upsert(cfg.variants_table, variant_rows)
        products_fetched += len(product_rows)
        variants_fetched += len(variant_rows)

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
        log.info("page synced", page=pages, products=len(product_rows), variants=len(variant_rows))

    return DrainResult(
        pages=pages,
        products_fetched=products_fetched,
        variants_fetched=variants_fetched,
        products_written=products_written,
        variants_written=variants_written,
        cursor=cursor,
        complete=complete,
    )


def _build_rows(shopify, products, synced_at, tenancy):
    """The rows for one page: one product row each, one variant row per variant.

    ``variants_with_product`` is what fetches a product's variants past the
    nested page and attaches the parent, so the variant mapping can read
    ``product.id`` without a second lookup.
    """
    product_rows = [product_row(product, synced_at, tenancy) for product in products]
    variant_rows = [
        variant_row(variant, synced_at, tenancy)
        for product in products
        for variant in variants_with_product(shopify, product)
    ]
    return product_rows, variant_rows
