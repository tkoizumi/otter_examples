"""Tests for the page loop.

A fake Shopify client, a fake sink and an injected clock, so the paths that
matter are reachable without a store, a database or a sleep: the budget stop,
the page cap, the checkpoint-after-write ordering, a rejected insert, and the two
ways a paging response can be wrong.

Checkpoint ordering is the one worth being explicit about. The cursor is saved
*after* the rows reach the sink, and not at all on the page that completes the
window -- the window's close is what commits. A checkpoint written before the
insert would let a crash skip a page it never wrote.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION_DIR = os.path.dirname(HERE)
# The shared connectors live beside this job's directory, under
# shopify_integrations/lib/python. Resolving them relative to this file is what
# lets the suite run from anywhere -- a bare `python3 -m unittest discover
# -s tests`, an editor, or CI -- with no PYTHONPATH set by the caller.
LIB_PYTHON = os.path.join(os.path.dirname(INTEGRATION_DIR), "lib", "python")

for path in (LIB_PYTHON, INTEGRATION_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from otter_connectors.clickhouse import ClickHouseError  # noqa: E402
from otter_connectors.shopify import ShopifyError  # noqa: E402

from clickhouse_sync import DrainResult, drain  # noqa: E402
from settings import Settings  # noqa: E402


def settings(**overrides):
    base = {
        "shopify_store": "example.myshopify.com",
        "clickhouse_url": "https://clickhouse.example:8443",
        "orders_table": "shopify_orders",
        "page_size": 100,
        "max_pages": 20,
        "overlap_seconds": 600,
        "budget_seconds": 240,
        "backfill_from": None,
        "backfill_days": 30,
        "dry_run": False,
        "workspace_id": "11111111-1111-1111-1111-111111111111",
        "connection_id": "22222222-2222-2222-2222-222222222222",
        "dataset_id": "33333333-3333-3333-3333-333333333333",
    }
    base.update(overrides)
    return Settings(**base)


def order(number):
    """An order node shaped like the GraphQL response."""
    return {
        "id": "gid://shopify/Order/%d" % number,
        "name": "#%d" % number,
        "email": "buyer%d@example.com" % number,
        "phone": "",
        "tags": [],
        "test": False,
        "createdAt": "2026-01-02T03:04:05Z",
        "processedAt": "2026-01-02T03:05:06Z",
        "updatedAt": "2026-02-03T04:05:06Z",
        "cancelledAt": None,
        "closedAt": None,
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "UNFULFILLED",
        "currencyCode": "USD",
        "customer": None,
        "currentSubtotalPriceSet": {"shopMoney": {"amount": "9.99"}},
        "currentTotalDiscountsSet": {"shopMoney": {"amount": "0.00"}},
        "currentShippingPriceSet": {"shopMoney": {"amount": "0.00"}},
        "currentTotalTaxSet": {"shopMoney": {"amount": "0.00"}},
        "currentTotalPriceSet": {"shopMoney": {"amount": "9.99", "currencyCode": "USD"}},
        "totalRefundedSet": {"shopMoney": {"amount": "0.00"}},
        "shippingAddress": {},
        "billingAddress": {},
    }


class FakeShopify:
    """Returns the queued pages in order, recording how it was asked."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def connection(self, query, variables=None, path="customers"):
        self.calls.append({"path": path, "variables": variables})
        if not self.pages:
            return [], {"hasNextPage": False, "endCursor": None}
        return self.pages.pop(0)


class FakeSink:
    def __init__(self):
        self.writes = []

    def upsert(self, rows):
        self.writes.append(rows)
        return len(rows)


class RejectingSink:
    """A sink whose insert fails, as ClickHouse's would."""

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def upsert(self, rows):
        self.calls += 1
        raise self.error


class FakeWindow:
    def __init__(self, start=None, cursor=None, dry_run=False):
        self.start = start or datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.cursor = cursor
        self.dry_run = dry_run
        self.checkpoints = []

    def checkpoint(self, cursor):
        if not self.dry_run and cursor:
            self.checkpoints.append(cursor)


class FakeLog:
    def __init__(self):
        self.events = []

    def info(self, event, **fields):
        self.events.append((event, fields))

    def warning(self, event, **fields):
        self.events.append((event, fields))

    def events_named(self, name):
        return [fields for event, fields in self.events if event == name]


def page(orders, *, has_next, cursor):
    return orders, {"hasNextPage": has_next, "endCursor": cursor}


class DrainTest(unittest.TestCase):
    def test_a_complete_window_drains_every_page(self):
        shopify = FakeShopify([
            page([order(1), order(2)], has_next=True, cursor="c1"),
            page([order(3)], has_next=False, cursor="c2"),
        ])
        sink = FakeSink()

        result = drain(FakeLog(), settings(), shopify, sink, FakeWindow(), deadline=1e9)

        self.assertEqual(result.pages, 2)
        self.assertEqual(result.orders_fetched, 3)
        self.assertEqual(result.orders_written, 3)
        self.assertTrue(result.complete)

    def test_each_page_writes_the_one_table(self):
        shopify = FakeShopify([page([order(1), order(2)], has_next=False, cursor="c1")])
        sink = FakeSink()

        drain(FakeLog(), settings(), shopify, sink, FakeWindow(), deadline=1e9)

        self.assertEqual(len(sink.writes), 1)
        self.assertEqual(len(sink.writes[0]), 2)
        self.assertEqual(sink.writes[0][0]["shopify_order_id"], "gid://shopify/Order/1")

    def test_the_cursor_is_checkpointed_only_between_pages(self):
        # The completing page is not checkpointed: the window's close commits
        # instead, and a cursor left behind would resume past the last page.
        shopify = FakeShopify([
            page([order(1)], has_next=True, cursor="c1"),
            page([order(2)], has_next=True, cursor="c2"),
            page([order(3)], has_next=False, cursor="c3"),
        ])
        window = FakeWindow()

        result = drain(FakeLog(), settings(), shopify, FakeSink(), window, deadline=1e9)

        self.assertEqual(window.checkpoints, ["c1", "c2"])
        # The completing page does not advance the cursor: there is nothing left
        # to resume, and the window's close is what commits the watermark.
        self.assertEqual(result.cursor, "c2")

    def test_a_dry_run_window_checkpoints_nothing(self):
        shopify = FakeShopify([
            page([order(1)], has_next=True, cursor="c1"),
            page([order(2)], has_next=False, cursor="c2"),
        ])
        window = FakeWindow(dry_run=True)

        drain(FakeLog(), settings(), shopify, FakeSink(), window, deadline=1e9)

        self.assertEqual(window.checkpoints, [])

    def test_an_empty_page_completes_the_window(self):
        shopify = FakeShopify([page([], has_next=True, cursor="c1")])
        sink = FakeSink()

        result = drain(FakeLog(), settings(), shopify, sink, FakeWindow(), deadline=1e9)

        self.assertTrue(result.complete)
        self.assertEqual(sink.writes, [])
        self.assertEqual(result.orders_written, 0)

    def test_the_page_cap_stops_a_long_window(self):
        pages = [page([order(n)], has_next=True, cursor="c%d" % n) for n in range(1, 6)]
        sink = FakeSink()

        result = drain(FakeLog(), settings(max_pages=2), FakeShopify(pages), sink,
                       FakeWindow(), deadline=1e9)

        self.assertEqual(result.pages, 2)
        self.assertFalse(result.complete)

    def test_an_exhausted_budget_stops_before_the_next_page(self):
        # The clock is injected, so the budget path needs no sleep and cannot
        # flake on a slow machine.
        ticks = iter([0.0, 1e9])
        shopify = FakeShopify([
            page([order(1)], has_next=True, cursor="c1"),
            page([order(2)], has_next=False, cursor="c2"),
        ])

        result = drain(
            FakeLog(), settings(), shopify, FakeSink(), FakeWindow(),
            deadline=100.0, now=lambda: next(ticks),
        )

        self.assertEqual(result.pages, 1)
        self.assertFalse(result.complete)
        self.assertEqual(shopify.calls[0]["variables"]["after"], None)

    def test_the_budget_stop_is_logged_so_a_partial_run_is_not_mistaken_for_a_quiet_one(self):
        log = FakeLog()
        ticks = iter([0.0, 1e9])

        drain(log, settings(), FakeShopify([page([order(1)], has_next=True, cursor="c1")]),
              FakeSink(), FakeWindow(), deadline=100.0, now=lambda: next(ticks))

        self.assertEqual(len(log.events_named("run budget reached; will continue on the next tick")), 1)

    def test_a_resumed_window_starts_from_its_cursor(self):
        shopify = FakeShopify([page([order(2)], has_next=False, cursor="c2")])
        window = FakeWindow(cursor="c1")

        drain(FakeLog(), settings(), shopify, FakeSink(), window, deadline=1e9)

        self.assertEqual(shopify.calls[0]["variables"]["after"], "c1")

    def test_the_window_start_is_the_search_filter(self):
        shopify = FakeShopify([page([order(1)], has_next=False, cursor="c1")])
        window = FakeWindow(start=datetime(2026, 2, 1, tzinfo=timezone.utc))

        drain(FakeLog(), settings(), shopify, FakeSink(), window, deadline=1e9)

        self.assertEqual(
            shopify.calls[0]["variables"]["query"],
            "status:any AND updated_at:>'2026-02-01T00:00:00Z'",
        )
        self.assertEqual(shopify.calls[0]["variables"]["sortKey"], "UPDATED_AT")

    def test_the_page_asks_shopify_for_the_orders_connection(self):
        # The path is the document's root field; a wrong one reads an empty
        # connection and looks like a store with no orders.
        shopify = FakeShopify([page([order(1)], has_next=False, cursor="c1")])

        drain(FakeLog(), settings(), shopify, FakeSink(), FakeWindow(), deadline=1e9)

        self.assertEqual(shopify.calls[0]["path"], "orders")
        self.assertEqual(shopify.calls[0]["variables"]["first"], 100)

    def test_a_non_advancing_cursor_aborts_rather_than_looping(self):
        shopify = FakeShopify([
            page([order(1)], has_next=True, cursor="c1"),
            page([order(2)], has_next=True, cursor="c1"),
        ])

        with self.assertRaises(ShopifyError) as caught:
            drain(FakeLog(), settings(), shopify, FakeSink(), FakeWindow(cursor="c1"), deadline=1e9)

        self.assertIn("non-advancing", str(caught.exception))

    def test_a_missing_cursor_on_a_page_with_more_aborts(self):
        shopify = FakeShopify([page([order(1)], has_next=True, cursor=None)])

        with self.assertRaises(ShopifyError):
            drain(FakeLog(), settings(), shopify, FakeSink(), FakeWindow(), deadline=1e9)

    def test_a_rejected_insert_fails_the_run_rather_than_checkpointing(self):
        # ClickHouse's message names the column and the value that broke it, so
        # the useful response is to stop. The cursor has not moved, so the retry
        # re-reads the page instead of skipping past rows that never landed.
        shopify = FakeShopify([
            page([order(1)], has_next=True, cursor="c1"),
            page([order(2)], has_next=False, cursor="c2"),
        ])
        window = FakeWindow()
        sink = RejectingSink(ClickHouseError("Code: 53. Cannot parse input"))

        with self.assertRaises(ClickHouseError):
            drain(FakeLog(), settings(), shopify, sink, window, deadline=1e9)

        self.assertEqual(sink.calls, 1)
        self.assertEqual(window.checkpoints, [])
        self.assertEqual(len(shopify.calls), 1)


class DrainResultTest(unittest.TestCase):
    def test_the_totals_are_carried_for_the_state_record(self):
        result = DrainResult(
            pages=1, orders_fetched=2, orders_written=2, cursor=None, complete=True,
        )
        self.assertEqual(result.orders_fetched, 2)
        self.assertEqual(result.orders_written, 2)


if __name__ == "__main__":
    unittest.main()
