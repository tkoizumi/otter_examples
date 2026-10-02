"""Tests for the query document and the window filter.

The query is an artifact, so it can be checked without a store. The check that
earns its place is the one holding ``source.ORDER_FIELDS`` to the document: a
field the mapping reads but the query does not select arrives as an empty column
and syncs silently wrong.

It is a shape check, not schema validation. There is no pulled Shopify schema in
this directory, so a field named in neither the query nor the field list is still
possible, and the first run against the live API is what proves the document.
"""

import os
import re
import sys
import unittest

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

import source  # noqa: E402
from otter_connectors.timeutil import parse_iso  # noqa: E402


class QueryDocumentTest(unittest.TestCase):
    def test_the_document_loads(self):
        self.assertIn("query CastorOrders", source.ORDERS_QUERY)

    def test_the_document_selects_every_field_the_mapping_reads(self):
        for field in source.ORDER_FIELDS:
            with self.subTest(field=field):
                # Matched on a word boundary rather than by containment:
                # ``email`` is a prefix of ``emailAddress``, so a plain
                # substring check would still pass after the top-level field had
                # been dropped from the document.
                self.assertRegex(source.ORDERS_QUERY, r"\b%s\b" % re.escape(field))

    def test_the_document_selects_page_info(self):
        # Without pageInfo the caller cannot tell a complete page from a
        # truncated one, so a partial page would end the window early and the
        # rest of the orders would never be fetched.
        self.assertIn("hasNextPage", source.ORDERS_QUERY)
        self.assertIn("endCursor", source.ORDERS_QUERY)

    def test_the_filter_and_the_sort_key_are_variables_not_literals(self):
        # The sibling document hardcodes its search string; this one must not.
        # The window is decided in source.py, and a literal here would silently
        # pin every run to that window.
        self.assertIn("$query: String", source.ORDERS_QUERY)
        self.assertIn("$sortKey: OrderSortKeys", source.ORDERS_QUERY)
        self.assertIn("query: $query", source.ORDERS_QUERY)
        self.assertIn("sortKey: $sortKey", source.ORDERS_QUERY)


class WindowFilterTest(unittest.TestCase):
    def test_the_filter_is_the_documented_shape(self):
        # Two clauses, and both are load-bearing. ``updated_at`` keeps the sync
        # incremental; ``status:any`` is what stops Shopify's default of open
        # orders only from silently dropping closed, cancelled and archived
        # ones. Shopify validates neither, so the shape is asserted here.
        start = parse_iso("2026-02-01T00:00:00Z")
        self.assertEqual(
            source.updated_since(start),
            "status:any AND updated_at:>'2026-02-01T00:00:00Z'",
        )

    def test_the_filter_asks_for_every_status(self):
        # Asserted separately from the whole string so a dropped clause fails
        # with a message naming the clause rather than a string diff.
        self.assertIn("status:any", source.updated_since(parse_iso("2026-02-01T00:00:00Z")))

    def test_the_filter_compares_updated_at(self):
        self.assertIn("updated_at:>", source.updated_since(parse_iso("2026-02-01T00:00:00Z")))

    def test_the_sort_key_is_one_shopify_accepts(self):
        # A variable's *value* is not type-checked, so this is a string a run
        # would only discover was wrong against the live API.
        self.assertEqual(source.SORT_KEY, "UPDATED_AT")


if __name__ == "__main__":
    unittest.main()
